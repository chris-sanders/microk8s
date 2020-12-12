#!/bin/env python3

from distutils.util import strtobool
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess
import datetime
import inspect
import os
import time

import yaml

import kubernetes
import pylxd


class NotFound(Exception):
    pass


class Node:
    """A test node used to run tests"""

    def __init__(self):
        self.cmd = Executor(self)
        self.snap = Snap(self)
        self.microk8s = Microk8s(self)
        self.kubernetes = Kubernetes(self)


class LXD(Node):
    """LXD Node type for testing in containers"""

    profile_name = "microk8s"

    def __init__(self, image=None, name=None):
        super().__init__()
        print("Creating a LXD node")
        self.client = pylxd.Client()

        if name:
            print(f"getting container {name}")
            self.container = self.client.containers.get(name)
        elif image:
            self.__setup_profile()
            print(f"creating container {image}")
            config = {
                'name': f'{self.__class__.__name__.lower()}-{self.__hash__()}',
                'source': {
                    'type': 'image',
                    "mode": "pull",
                    "server": "https://cloud-images.ubuntu.com/daily",
                    "protocol": "simplestreams",
                    'alias': image,
                },
                'profiles': ['default', self.profile_name],
            }
            self.container = self.client.containers.create(config=config, wait=True)

    def __setup_profile(self):
        """Setup microk8s profile if not present"""

        if self.client.profiles.exists(self.profile_name):
            return

        cwd = Path(__file__).parent
        pfile = cwd / 'lxc' / 'microk8s.profile'
        with pfile.open() as f:
            profile = yaml.safe_load(f)
        self.client.profiles.create(self.profile_name, profile["config"], profile["devices"])

    def start(self):
        """Start the node"""

        return self.container.start(wait=True)

    def stop(self):
        """Stop the node"""

        return self.container.stop(wait=True)

    def delete(self):
        """Delete the node"""

        return self.container.delete()

    def check_output(self, cmd):
        """Check execution of a command"""
        exit_code, stdout, stderr = self.container.execute(cmd)
        CompletedProcess(cmd, exit_code, stdout, stderr).check_returncode()

        return stdout


class XenialLxd(LXD):
    """Xenial LXD Node"""

    def __init__(self, name=None):
        if name:
            super().__init__(name=name)
        else:
            super().__init__(image="xenial/amd64")


class BionicLxd(LXD):
    """Bionic LXD Node"""

    def __init__(self, name=None):
        if name:
            super().__init__(name=name)
        else:
            super().__init__(image="bionic/amd64")


class FocalLxd(LXD):
    """Focal LXD Node"""

    def __init__(self, name=None):
        if name:
            super().__init__(name=name)
        else:
            super().__init__(image="focal/amd64")


class Executor:
    """Node aware command executor"""

    prefix = []

    def __init__(self, node):
        """Initialize an executor"""
        self.node = node

    def run(self, cmd):
        full_cmd = self.prefix + cmd

        return self.node.check_output(full_cmd)

    def run_until_success(self, cmd, timeout=60):
        """
        Run a command until it succeeds or times out.
        Args:
            cmd: Command to run
            timeout: Time out in seconds

        Returns: The string output of the command

        """
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=timeout)

        while True:
            try:
                output = self.run(cmd)

                return output
            except CalledProcessError:
                if datetime.datetime.now() > deadline:
                    raise
                print("Retrying {}".format(cmd))
                time.sleep(3)


class Snap(Executor):
    """Node aware SNAP executor"""

    prefix = ['snap']

    def install(self, snap, channel, classic=False):
        """Install a snap"""
        cmd = ["install", f"{snap}", f"--channel={channel}"]

        if classic:
            cmd.append("--classic")
        self.run_until_success(cmd)


class Microk8s(Executor):
    """Node aware MicroK8s executor"""

    prefix = ["/snap/bin/microk8s"]

    @property
    def config(self):
        """Microk8s config"""
        cmd = ["config"]

        return self.run_until_success(cmd)

    def start(self):
        """Start microks"""
        cmd = ["start"]

        return self.run_until_success(cmd)

    def status(self):
        """Microk8s status"""
        cmd = ["status"]

        return self.run_until_success(cmd)

    def enable(self, addons):
        """Enable a addons"""
        cmd = ["enable"]
        cmd.extend(addons)

        result = self.run_until_success(cmd)

        return result

    def wait_until_running(self, timeout=60):
        """Wait until the status returns running"""
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=timeout)

        while True:
            status = self.status()

            if "microk8s is running" in status:
                return
            elif datetime.datetime.now > deadline:
                raise TimeoutError("Timeout waiting for microk8s status")
            time.sleep(1)

    def wait_until_service_running(self, service, timeout=60):
        """Wait until a microk8s service is running"""

        deadline = datetime.datetime.now() + datetime.timedelta(seconds=timeout)
        cmd = [
            "systemctl",
            "is-active",
            f"snap.microk8s.daemon-{service}.service",
        ]

        while True:
            service_status = self.node.cmd.run_until_success(cmd)

            if "active" in service_status:
                return
            elif datetime.datetime.now() > deadline:
                raise TimeoutError(f"Timeout waiting for {service} to become active")
            time.sleep(1)


class RetryWrapper:
    """Generic class for retyring method calls on an object"""

    def __init__(self, object, exception=Exception, timeout=60):
        self.object = object
        self.exception = exception
        self.timeout = timeout

    def __getattribute__(self, name, *args, **kwargs):
        object = super().__getattribute__("object")
        exception = super().__getattribute__("exception")
        timeout = super().__getattribute__("timeout")

        if not hasattr(object, name):
            raise AttributeError(f"No {name} on {type(object)}")
        else:
            attr = getattr(object, name)

            if not inspect.ismethod(attr):
                return attr

            def wrapped(*args, **kwargs):
                deadline = datetime.datetime.now() + datetime.timedelta(seconds=timeout)

                while True:
                    try:
                        result = attr(*args, **kwargs)

                        return result
                    except exception as e:
                        if datetime.datetime.now() >= deadline:
                            raise e
                        time.sleep(1)

            return wrapped


class Kubernetes:
    """Node aware Kubernetes api commands"""

    def __init__(self, node):
        """Initialize the api"""
        self.node = node
        self._api = None

    @property
    def api(self):
        if self._api:

            return self._api

        config = kubernetes.client.configuration.Configuration.get_default_copy()
        # config.retries = 60
        mk8s_config = yaml.safe_load(self.node.microk8s.config)
        kubernetes.config.load_kube_config_from_dict(mk8s_config, client_configuration=config)
        api_client = kubernetes.client.ApiClient(configuration=config)
        self._raw_api = kubernetes.client.CoreV1Api(api_client=api_client)
        self._api = RetryWrapper(self._raw_api, Exception)

        return self._api

    def wait_nodes_ready(self, count, timeout=60):
        """Wait for nodes to become ready"""
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=timeout)
        nodes = self.api.list_node().items

        while True:
            ready_count = 0

            for node in nodes:
                for condition in node.status.conditions:
                    if condition.type == "Ready" and condition.status == "True":
                        ready_count += 1

            if ready_count >= count:
                return count
            elif datetime.datetime.now() > deadline:
                raise TimeoutError(f"Timeout waiting {ready_count} of {count} Ready")

    def create_namespace(self, namespace):
        """Create a namespace"""

        metadata = kubernetes.client.V1ObjectMeta(name=namespace)
        namespace = kubernetes.client.V1Namespace(metadata=metadata)
        api_response = self.api.create_namespace(namespace)

        return api_response

    def get_service_cluster_ip(self, namespace, name):
        """Get an IP for a service by name"""
        service_list = self.api.list_namespaced_service(namespace)

        if not service_list.items:
            raise NotFound(f"No services in namespace {namespace}")

        for service in service_list.items:
            if service.metadata.name == name:
                return service.spec.cluster_ip

        raise NotFound(f"cluster_ip not found for {name} in {namespace}")

    def get_pod_by_label(self, namespace, label):
        """Get a pod by lable"""
        pod_list = self.api.list_namespaced_pod(namespace, label_selector=label)

        if not pod_list.items:
            raise NotFound(f"No pods in namespace {namespace} with label {label}")

        return pod_list.items

    def all_containers_ready(self, namespace, label=None):
        """Check if all containers in all pods are ready"""

        ready = True

        if label:
            pods = self.api.list_namespaced_pod(namespace, label_selector=label)
        else:
            pods = self.api.list_namespaced_pod(namespace)

        if not len(pods.items):
            return False

        for pod in pods.items:
            try:
                for container in pod.status.container_statuses:
                    ready = ready & container.ready
            except TypeError:
                return False

        return ready

    def wait_containers_ready(self, namespace, label=None, timeout=60):
        """Wait up to timeout for all containers to be ready."""
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=timeout)

        while True:
            if self.all_containers_ready(namespace, label):
                return
            elif datetime.datetime.now() > deadline:
                raise TimeoutError(f"Timeout waiting for containers in {namespace}")
            else:
                time.sleep(1)


class InstallTests:
    """MicroK8s Install Tests"""

    node_type = None
    keep_node = bool(strtobool(os.environ.get("MK8S_KEEP_NODE", "false")))
    existing_node = os.environ.get("MK8S_EXISTING_NODE", None)
    install_version = os.environ.get("MK8S_INSTALL_VERSION", "beta")

    def setup_class(self):
        """Setup the tests"""
        print("Setting up Upgrade tests")

        if self.existing_node:
            print(f"Using existing node: {self.existing_node}")
            self.node = self.node_type(name=self.existing_node)
        else:
            print("Creating new node")
            self.node = self.node_type()
            self.node.start()

    def teardown_class(self):
        if self.keep_node:
            return
        self.node.stop()
        self.node.delete()

    def test_collection(self):
        """Test that this test is collected"""

        return True

    def test_node_setup(self):
        """Test that expceted nodes exist"""
        assert isinstance(self.node, Node)

    def test_snap_install(self):
        """Test installing a snap"""
        self.node.snap.install("microk8s", channel=self.install_version, classic=True)

    def test_start_microk8s(self):
        """Test starting microk8s"""
        self.node.microk8s.start()
        self.node.microk8s.wait_until_running()
        status = self.node.microk8s.status()
        assert "microk8s is running" in status

    def test_get_kubeconfig(self):
        """Test retreiving the kubeconfig"""
        config = yaml.safe_load(self.node.microk8s.config)
        assert config["clusters"][0]["name"] == "microk8s-cluster"

    def test_nodes_ready(self):
        """Test nodes are ready"""
        ready = self.node.kubernetes.wait_nodes_ready(1)
        assert ready == 1

    def test_dns(self):
        """Test dns_dashboard addons"""
        result = self.node.microk8s.enable(["dns"])
        assert "Nothing to do for" not in result
        self.node.kubernetes.api.api_client.close()
        self.node.kubernetes.wait_containers_ready(
            "kube-system", label="k8s-app=kube-dns", timeout=120
        )

    def test_dashboard(self):
        """Test dashboard addon"""
        self.node.microk8s.enable(["dashboard"])
        self.node.kubernetes.wait_containers_ready(
            "kube-system", label="k8s-app=kubernetes-dashboard", timeout=90,
        )
        self.node.kubernetes.wait_containers_ready(
            "kube-system", label="k8s-app=dashboard-metrics-scraper"
        )


class UpgradeTests(InstallTests):
    """Upgrade after an install"""

    upgrade_version = os.environ.get("MK8S_UPGRADE_VERSION", "edge")


class TestXenialUpgrade(UpgradeTests):
    """Run Upgrade tests on a Xeinal node"""

    node_type = XenialLxd


class TestBionicUpgrade(UpgradeTests):
    """Run Upgrade tests on a Bionic node"""

    node_type = BionicLxd


# class TestFocalUpgrade(UpgradeTests):
#     """Run Upgrade tests on a Focal node"""
#
#     node_type = FocalLxd
