#!/bin/env python3

from distutils.util import strtobool
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess
import datetime
import inspect
import os
import time

from jinja2 import Template
import requests
import yaml

import kubernetes
import pylxd


class NotFound(Exception):
    pass


class Node:
    """A test node with executors"""

    def __init__(self):
        self.cmd = Executor(self)
        self.snap = Snap(self)
        self.kubectl = Kubectl(self)
        self.docker = Docker(self)
        self.microk8s = Microk8s(self)
        self.kubernetes = Kubernetes(config=self.microk8s.get_config)


class Lxd(Node):
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
        try:
            CompletedProcess(cmd, exit_code, stdout, stderr).check_returncode()
        except CalledProcessError as e:
            print(f"Stdout: {stdout}\r" f"Stderr: {stderr}\r")
            raise e

        return stdout

    def write(self, dest, contents):
        """Write contents at destination on node"""

        return self.container.files.put(dest, contents)

    def get_primary_address(self):
        """Get the primary interface ip address"""

        return self.container.state().network['eth0']['addresses'][0]['address']


class XenialLxd(Lxd):
    """Xenial LXD Node"""

    def __init__(self, name=None):
        if name:
            super().__init__(name=name)
        else:
            super().__init__(image="xenial/amd64")


class BionicLxd(Lxd):
    """Bionic LXD Node"""

    def __init__(self, name=None):
        if name:
            super().__init__(name=name)
        else:
            super().__init__(image="bionic/amd64")


class FocalLxd(Lxd):
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
                time.sleep(1)


class Snap(Executor):
    """Node aware SNAP executor"""

    prefix = ['snap']

    def install(self, snap, channel, classic=False):
        """Install a snap"""
        cmd = ["install", f"{snap}", f"--channel={channel}"]

        if classic:
            cmd.append("--classic")
        self.run_until_success(cmd)


class Docker(Executor):
    """Node aware Docker executor"""

    prefix = ["docker"]

    def cmd(self, args):
        self.run_until_success(args)


class Addon:
    """
    Base class for testing Microk8s addons.
    Validation requires a Kubernetes instance on the node
    """

    name = None

    def __init__(self, node):
        self.node = node

    def enable(self):
        return self.node.microk8s.enable([self.name])

    def disable(self):
        return self.node.microk8s.disable([self.name])

    def apply_template(self, template, context={}, yml=False):
        # Create manifest
        cwd = Path(__file__).parent
        template = cwd / 'templates' / template
        with template.open() as f:
            rendered = Template(f.read()).render(context)
        render_path = f"/tmp/{template.stem}.yaml"
        self.node.write(render_path, rendered)

        return self.node.microk8s.kubectl.apply(["-f", render_path], yml=yml)

    def delete_template(self, template, context={}, yml=False):
        path = Path(template)
        render_path = f"/tmp/{path.stem}.yaml"

        return self.node.microk8s.kubectl.delete(["-f", render_path], yml=yml)


class Dns(Addon):
    """Microk8s dns addon"""

    name = "dns"

    def validate(self):
        self.node.kubernetes.wait_containers_ready(
            "kube-system", label="k8s-app=kube-dns", timeout=120
        )


class Dashboard(Addon):
    """Dashboard addon"""

    name = "dashboard"

    def validate(self):
        self.node.kubernetes.wait_containers_ready(
            "kube-system", label="k8s-app=kubernetes-dashboard", timeout=90,
        )
        self.node.kubernetes.wait_containers_ready(
            "kube-system", label="k8s-app=dashboard-metrics-scraper"
        )
        name = "https:kubernetes-dashboard:"
        result = self.node.kubernetes.get_service_proxy(name=name, namespace="kube-system")
        assert "Kubernetes Dashboard" in result


class Storage(Addon):
    """Storage addon"""

    name = "storage"

    def validate(self):
        self.node.kubernetes.wait_containers_ready(
            "kube-system", label="k8s-app=hostpath-provisioner"
        )
        claim = self.node.kubernetes.create_pvc(
            "testpvc", "kube-system", storage_class="microk8s-hostpath", wait=True
        )
        assert claim.spec.storage_class_name == 'microk8s-hostpath'
        self.node.kubernetes.delete_pvc("testpvc", "kube-system")


class Ingress(Addon):
    """Ingress addon"""

    name = "ingress"

    def validate(self):
        # TODO: Is this still needed?
        # self.node.kubernetes.wait_containers_ready("default", label="app=default-http-backend")
        # self.node.kubernetes.wait_containers_ready("default", label="name=nginx-ingress-microk8s")
        self.node.kubernetes.wait_containers_ready("ingress", label="name=nginx-ingress-microk8s")

        # Create manifest
        context = {
            "arch": "amd64",
            "address": self.node.get_primary_address(),
        }
        self.apply_template("ingress.j2", context)

        self.node.kubernetes.wait_containers_ready("default", label="app=microbot")
        nip_addresses = self.node.kubernetes.wait_ingress_ready("microbot-ingress-nip", "default")
        xip_addresses = self.node.kubernetes.wait_ingress_ready("microbot-ingress-xip", "default")
        assert "127.0.0.1" in nip_addresses[0].ip
        assert "127.0.0.1" in xip_addresses[0].ip

        deadline = datetime.datetime.now() + datetime.timedelta(seconds=30)
        while True:
            resp = requests.get(f"http://microbot.{context['address']}.nip.io/")
            if resp.status_code == 200 or datetime.datetime.now() > deadline:
                break
            time.sleep(1)
        assert resp.status_code == 200
        assert "microbot.png" in resp.content.decode("utf8")

        deadline = datetime.datetime.now() + datetime.timedelta(seconds=30)
        while True:
            resp = requests.get(f"http://microbot.{context['address']}.xip.io/")
            if resp.status_code == 200 or datetime.datetime.now() > deadline:
                break
            time.sleep(1)
        assert resp.status_code == 200
        assert "microbot.png" in resp.content.decode("utf8")

        self.delete_template("ingress.j2", context)


class Gpu(Addon):
    """Gpu addon"""

    name = "gpu"

    def validate(self):
        self.node.kubernetes.wait_containers_ready(
            "kube-system", label="name=nvidia-device-plugin-ds"
        )

        # Create manifest
        context = {}
        self.apply_template("cuda-add.j2", context)
        # TODO: Finish validator on hardware with GPU
        self.delete_template("cuda-add.j2", context)


class Registry(Addon):
    """Registry addon"""

    name = "registry"

    def validate(self):
        self.node.kubernetes.wait_containers_ready("container-registry", label="app=registry")
        claim = self.node.kubernetes.wait_pvc_phase("registry-claim", "container-registry")
        assert "20Gi" in claim.status.capacity["storage"]

        self.node.docker.cmd(["pull", "busybox"])
        self.node.docker.cmd(["tag", "busybox", "localhost:32000/my-busybox"])
        self.node.docker.cmd(["push", "localhost:32000/my-busybox"])

        context = {"image": "localhost:32000/my-busybox"}
        self.apply_template("bbox.j2", context)

        self.node.kubernetes.wait_containers_ready("default", field="metadata.name=busybox")
        pods = self.node.kubernetes.get_pods("default", field="metadata.name=busybox")
        assert pods[0].spec.containers[0].image == "localhost:32000/my-busybox"

        self.delete_template("bbox.j2", context)


class MetricsServer(Addon):

    name = "metrics-server"

    def validate(self):
        self.node.kubernetes.wait_containers_ready("kube-system", label="k8s-app=metrics-server")
        metrics_uri = "/apis/metrics.k8s.io/v1beta1/pods"
        reply = self.node.kubernetes.get_raw_api(metrics_uri)
        assert reply["kind"] == "PodMetricsList"


class Fluentd(Addon):

    name = "fluentd"

    def validate(self):
        self.node.kubernetes.wait_containers_ready(
            "kube-system", field="metadata.name=elasticsearch-logging-0", timeout=300
        )
        self.node.kubernetes.wait_containers_ready(
            "kube-system", label="k8s-app=fluentd-es", timeout=300
        )
        self.node.kubernetes.wait_containers_ready(
            "kube-system", label="k8s-app=kibana-logging", timeout=300
        )


class Jaeger(Addon):

    name = "jaeger"

    def validate(self):
        self.node.kubernetes.wait_containers_ready("default", label="name=jaeger-operator")
        self.node.kubernetes.wait_ingress_ready("simplest-query", "default")


class Metallb(Addon):

    name = "metallb"

    def enable(self, ip_ranges=None):
        if not ip_ranges:
            return self.node.microk8s.enable([self.name])
        else:
            return self.node.microk8s.enable([f"{self.name}:{ip_ranges}"])

    def validate(self, ip_ranges=None):
        context = {}
        self.apply_template("load-balancer.j2", context)
        ip = self.node.kubernetes.wait_load_balancer_ip("default", "example-service")

        if ip_ranges:
            assert ip in ip_ranges
        self.delete_template("load-balancer.j2", context)


class Kubectl(Executor):
    """Node aware Microk8s Kubectl executor"""

    prefix = ["kubectl"]

    def __init__(self, *args, prefix=None, **kwargs):
        super().__init__(*args, **kwargs)

        if prefix and isinstance(prefix, list):
            self.prefix = prefix + self.prefix
            print(f"Using modified prefix: {self.prefix}")

    def result(self, cmds, yml):
        """Return commands optionally as yaml"""

        if yml:
            cmds.append("-oyaml")

            return yaml.safe_load(self.run_until_success(cmds))

        return self.run_until_success(cmds)

    def get(self, args, yml=True):

        cmd = ["get"]
        cmd.extend(args)

        return self.result(cmd, yml)

    def apply(self, args, yml=True):

        cmd = ["apply"]
        cmd.extend(args)

        return self.result(cmd, yml)

    def delete(self, args, yml=True):
        cmd = ["delete"]
        cmd.extend(args)

        return self.result(cmd, yml)


class Microk8s(Executor):
    """Node aware MicroK8s executor"""

    prefix = [
        "/snap/bin/microk8s",
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.kubectl = Kubectl(self.node, prefix=self.prefix)
        self.dns = Dns(self.node)
        self.dashboard = Dashboard(self.node)
        self.storage = Storage(self.node)
        self.ingress = Ingress(self.node)
        self.gpu = Gpu(self.node)
        self.registry = Registry(self.node)
        self.metrics_server = MetricsServer(self.node)
        self.fluentd = Fluentd(self.node)
        self.jaeger = Jaeger(self.node)
        self.metallb = Metallb(self.node)

    @property
    def config(self):
        """Microk8s config"""
        cmd = ["config"]

        return self.run_until_success(cmd)

    def get_config(self):
        """Return this nodes config"""

        return self.config

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
            elif datetime.datetime.now() > deadline:
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
    """Kubernetes api commands"""

    def __init__(self, config):
        """
        Initialize the api
        Config can be provided as a dictionary or a callable that will be evaluated when the
        api is used the first time. If callable is provided the output will be run through
        yaml_safeload to produce the config.
        """

        self._config = config
        self._api = None
        self._api_network = None

    @property
    def api(self):
        if self._api:

            return self._api

        config = kubernetes.client.configuration.Configuration.get_default_copy()
        # config.retries = 60
        local_config = self.config
        kubernetes.config.load_kube_config_from_dict(local_config, client_configuration=config)
        self.api_client = kubernetes.client.ApiClient(configuration=config)
        self._raw_api = kubernetes.client.CoreV1Api(api_client=self.api_client)
        self._api = RetryWrapper(self._raw_api, Exception)

        return self._api

    @property
    def api_network(self):
        if self._api_network:
            return self._api_network

        self.api  # Ensure the core api has been setup
        self._raw_api_network = kubernetes.client.NetworkingV1beta1Api(api_client=self.api_client)
        self._api_network = RetryWrapper(self._raw_api_network, Exception)

        return self._api_network

    @property
    def config(self):
        """Return config"""

        if callable(self._config):
            self._config = yaml.safe_load(self._config())

        return self._config

    def get_raw_api(self, url):
        self.api
        resp = self.api_client.call_api(
            url, 'GET', auth_settings=['BearerToken'], response_type='yaml', _preload_content=False
        )

        return yaml.safe_load(resp[0].data.decode('utf8'))

    def create_from_yaml(self, yaml_file, verbose=False, namespace="default"):
        """Create objcets from yaml input"""

        if not self.api_client:
            self.api  # Accessing the api creates an api_client

        return kubernetes.utils.create_from_yaml(
            k8s_client=self.api_client, yaml_file=yaml_file, verbose=verbose, namespace=namespace
        )

    def get_service_proxy(self, name, namespace, path=None):
        """Return a GET call to a proxied service"""

        if path:
            response = self.api.connect_get_namespaced_service_proxy(name, namespace, path)
        else:
            response = self.api.connect_get_namespaced_service_proxy(name, namespace)

        return response

    def create_pvc(
        self,
        name,
        namespace,
        storage="1G",
        access=["ReadWriteOnce"],
        storage_class=None,
        wait=False,
    ):
        """Create a PVC"""
        claim = kubernetes.client.V1PersistentVolumeClaim()
        spec = kubernetes.client.V1PersistentVolumeClaimSpec()
        metadata = kubernetes.client.V1ObjectMeta()
        resources = kubernetes.client.V1ResourceRequirements()
        metadata.name = name
        resources.requests = {}
        resources.requests["storage"] = storage
        spec.access_modes = access
        spec.resources = resources

        if storage_class:
            spec.storage_class_name = storage_class
        claim.metadata = metadata
        claim.spec = spec

        if wait:
            self.api.create_namespaced_persistent_volume_claim(namespace, claim)

            return self.wait_pvc_phase(name, namespace)
        else:
            return self.api.create_namespaced_persistent_volume_claim(namespace, claim)

    def delete_pvc(self, name, namespace):
        """Delete a PVC"""

        return self.api.delete_namespaced_persistent_volume_claim(name, namespace)

    def wait_pvc_phase(self, name, namespace, phase="Bound", timeout=60):
        """Wait for a PVC to enter the given phase"""
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=timeout)

        while True:
            claim = self.api.read_namespaced_persistent_volume_claim_status(name, namespace)

            if claim.status.phase == phase:
                return claim
            elif datetime.datetime.now() > deadline:
                raise TimeoutError(f"Timeout waiting for {name} to become {phase}")
            time.sleep(0.5)

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

    def get_service_load_balancer_ip(self, namespace, name):
        """Get an LB IP for a service by name"""
        service_list = self.api.list_namespaced_service(namespace)

        if not service_list.items:
            raise NotFound(f"No services in namespace {namespace}")

        for service in service_list.items:
            if service.metadata.name == name:
                try:
                    return service.status.load_balancer.ingress[0].ip
                except TypeError:
                    pass

        raise NotFound(f"load_balancer_ip not found for {name} in {namespace}")

    def wait_load_balancer_ip(self, namespace, name, timeout=60):
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=timeout)

        while True:
            try:
                ip = self.get_service_load_balancer_ip(namespace, name)

                if ip:
                    return ip
            except NotFound:
                pass

            if datetime.datetime.now() > deadline:
                raise TimeoutError(f"Timeout waiting for {name} in {namespace}")
            time.sleep(1)

    def get_pods(self, namespace, label=None, field=None):
        """Get pod list"""
        pod_list = self.api.list_namespaced_pod(
            namespace, label_selector=label, field_selector=field
        )

        if not pod_list.items:
            raise NotFound(f"No pods in namespace {namespace} with label {label}")

        return pod_list.items

    def all_containers_ready(self, namespace, label=None, field=None):
        """Check if all containers in all pods are ready"""

        ready = True

        pods = self.api.list_namespaced_pod(namespace, label_selector=label, field_selector=field)

        if not len(pods.items):
            return False

        for pod in pods.items:
            try:
                for container in pod.status.container_statuses:
                    ready = ready & container.ready
            except TypeError:
                return False

        return ready

    def wait_containers_ready(self, namespace, label=None, field=None, timeout=60):
        """Wait up to timeout for all containers to be ready."""
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=timeout)

        while True:
            if self.all_containers_ready(namespace, label, field):
                return
            elif datetime.datetime.now() > deadline:
                raise TimeoutError(f"Timeout waiting for containers in {namespace}")
            else:
                time.sleep(1)

    def wait_ingress_ready(self, name, namespace, timeout=60):
        """Wait for an ingress to get an address"""
        deadline = datetime.datetime.now() + datetime.timedelta(seconds=timeout)

        while True:
            result = self.api_network.read_namespaced_ingress(name, namespace)

            if result.status.load_balancer.ingress is not None:
                return result.status.load_balancer.ingress
            elif datetime.datetime.now() > deadline:
                raise TimeoutError(f"Timeout waiting for Ingress {name}")
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
        result = self.node.microk8s.dns.enable()
        assert "Nothing to do for" not in result
        self.node.microk8s.dns.validate()

    def test_dashboard(self):
        """Test dashboard addon"""
        self.node.microk8s.dashboard.enable()
        self.node.microk8s.dashboard.validate()

    def test_storage(self):
        """Test storage addon"""
        self.node.microk8s.storage.enable()
        self.node.microk8s.storage.validate()

    def test_ingress(self):
        """Test ingress addon"""
        self.node.microk8s.ingress.enable()
        self.node.microk8s.ingress.validate()

    # def test_gpu(self):
    #     """Test gpu addon"""
    #     self.node.microk8s.gpu.enable()
    #     self.node.microk8s.gpu.validate()

    def test_registry(self):
        """Test registry addon"""
        self.node.snap.install("docker", channel="stable", classic=True)
        self.node.microk8s.registry.enable()
        self.node.microk8s.registry.validate()

    def test_metrics_erver(self):
        """Test metrics-server addon"""
        self.node.microk8s.metrics_server.enable()
        self.node.microk8s.metrics_server.validate()

    def test_fluentd(self):
        """Test fluentd addon"""
        self.node.microk8s.fluentd.enable()
        self.node.microk8s.fluentd.validate()

    def test_jaeger(self):
        """Test Jaeger"""
        self.node.microk8s.jaeger.enable()
        self.node.microk8s.jaeger.validate()

    def test_metallb(self):
        """Test Metallb"""
        ip_ranges = "192.168.0.105-192.168.0.105,192.168.0.110-192.168.0.111,192.168.1.240/28"
        self.node.microk8s.metallb.enable(ip_ranges)
        self.node.microk8s.metallb.validate(ip_ranges)


class UpgradeTests(InstallTests):
    """Upgrade after an install"""

    upgrade_version = os.environ.get("MK8S_UPGRADE_VERSION", "edge")


class TestXenialUpgrade(UpgradeTests):
    """Run Upgrade tests on a Xeinal node"""

    node_type = XenialLxd


# class TestBionicUpgrade(UpgradeTests):
#     """Run Upgrade tests on a Bionic node"""
#
#     node_type = BionicLxd


# class TestFocalUpgrade(UpgradeTests):
#     """Run Upgrade tests on a Focal node"""
#
#     node_type = FocalLxd
