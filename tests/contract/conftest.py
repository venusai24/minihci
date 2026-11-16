"""Driver contract fixtures. FakeDriver always runs; libvirt needs MINIHCI_REAL_HOSTS=host1,host2."""

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from minihci.driver.base import HostDriver

BASE_IMAGE = os.environ.get("MINIHCI_REAL_BASE_IMAGE", "/srv/minihci/base/base.qcow2")
DISK_DIR = os.environ.get("MINIHCI_REAL_DISK_DIR", "/srv/minihci/images")


@dataclass
class Env:
    driver: HostDriver
    uri1: str
    uri2: str
    base_image: str
    disk_dir: str

    def name(self) -> str:
        return f"mhci-contract-{uuid.uuid4().hex[:8]}"

    def disk(self, name: str) -> str:
        return f"{self.disk_dir}/{name}.qcow2"


def _fake() -> Env:
    from minihci.driver.fake import FakeDriver

    d = FakeDriver()
    u1, u2 = "fake://h1", "fake://h2"
    d.add_host(u1, 8, 16384)
    d.add_host(u2, 8, 16384)
    d.create_disk_overlay("/base/b.qcow2", "/base/b.qcow2")  # stand-in base image
    return Env(d, u1, u2, "/base/b.qcow2", "/vms")


def _libvirt() -> Env:
    from minihci.driver.libvirt import LibvirtDriver

    hosts = [h.strip() for h in os.environ["MINIHCI_REAL_HOSTS"].split(",") if h.strip()]
    uris = [f"qemu+ssh://root@{h}/system" for h in hosts]
    d = LibvirtDriver(storage_uris=uris, image_root=os.path.dirname(BASE_IMAGE), storage_root=DISK_DIR)
    return Env(d, uris[0], uris[-1], BASE_IMAGE, DISK_DIR)


@pytest.fixture(
    params=["fake", pytest.param("libvirt", marks=pytest.mark.real_hosts)],
)
def env(request: pytest.FixtureRequest) -> Iterator[Env]:
    if request.param == "fake":
        yield _fake()
        return
    if not os.environ.get("MINIHCI_REAL_HOSTS"):
        pytest.skip("MINIHCI_REAL_HOSTS not set")
    yield _libvirt()
