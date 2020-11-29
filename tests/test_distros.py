#!/bin/env python3

import time

# import pytest
import pylxd


class Node:
    """A test node used to run functional tests"""

    pass


# @pytest.mark.lxd
class LXD(Node):
    """LXD Node type for testing in containers"""

    def __init__(self, image):
        print("Creating a LXD node")
        print(f"Image: {image}")
        client = pylxd.Client()
        config = {
            'name': f'{self.__class__.__name__}-{self.__hash__()}',
            'source': {
                'type': 'image',
                "mode": "pull",
                "server": "https://cloud-images.ubuntu.com/daily",
                "protocol": "simplestreams",
                'alias': image,
            },
            # 'profiles': ['profilename'],
        }

        self.container = client.containers.create(config=config, wait=True)

    def start(self):
        """Start the node"""

        return self.container.start(wait=True)

    def stop(self):
        """Stop the node"""

        return self.container.stop(wait=True)

    def delete(self):
        """Delete the node"""

        return self.container.delete()

    def check_call(self):
        """Check execution of a command"""

        return True


# @pytest.mark.localhost
# class Localhost(Node):
#     """Localhost Node type for testing localy"""
#
#     pass


class Executor:
    """Node aware command execution"""

    def __init__(self, node):
        """Initialize an executor"""

        pass


class XenialLxd(LXD):
    """Sets up Xenial"""

    def __init__(self):
        super().__init__("xenial/amd64")


class BionicLxd(LXD):
    """Sets up Xenial"""

    def __init__(self):
        super().__init__("bionic/amd64")


class UpgradeTests:
    """Upgrade Test Mixin"""

    node_type = None

    def setup_class(self):
        """Setup the tests"""
        print("Setting up Upgrade tests")
        self.node = self.node_type()
        assert isinstance(self.node, Node)
        self.node.start()
        # self.ex = Executor(self.node)
        print(f"Node: {self.node}")

    def teardown_class(self):
        print("Tearing down Upgrade tests")
        self.node.stop()
        self.node.delete()

    def test_collection(self):
        """Test that this test is collected"""

        return True

    def test_node_setup(self):
        """Test that expceted nodes exist"""
        assert self.node

    # def test_snap_install(self):
    #     """Test installing a snap"""
    #     self.node.snap.install("microk8s", channel="1.19/stable")
    #     pass

    def test_long_tests(self):
        """This is just a tests"""
        time.sleep(1)


class TestXenialUpgrade(UpgradeTests):
    """Run Upgrade tests on a Xeinal node"""

    node_type = XenialLxd


class TestBionicLXDUpgrade(UpgradeTests):
    """Run Upgrade tests on a Xeinal node"""

    node_type = BionicLxd

    pass


# class TestLocalhostUpgrade(Localhost, UpgradeTests):
#     """Run upgrade tests on local host"""
#
#     pass
