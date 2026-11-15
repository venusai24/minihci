import threading
import time

import pytest

from minihci.config import Settings
from minihci.driver import build_driver
from minihci.driver.base import (
    DomainExists,
    DomainNotFound,
    DomainSpec,
    DomainState,
    DriverError,
    HostDriver,
    HostInfo,
    HostUnreachable,
)
from minihci.driver.fake import FakeDriver

URI = "qemu+ssh://a/system"
SPEC = DomainSpec(name="vm1", vcpus=1, mem_mb=512, disk_path="/srv/vm1.qcow2")


@pytest.fixture
def drv():
    d = FakeDriver()
    d.add_host(URI, cpu_total=8, mem_total_mb=16384)
    return d


def test_satisfies_protocol(drv):
    assert isinstance(drv, HostDriver)


def test_probe(drv):
    assert drv.probe_host(URI) == HostInfo(cpu_total=8, mem_total_mb=16384)
    with pytest.raises(HostUnreachable):
        drv.probe_host("qemu+ssh://nope/system")


def test_domain_lifecycle(drv):
    assert drv.domain_state(URI, "vm1") is None
    drv.define_domain(URI, SPEC)
    assert drv.domain_state(URI, "vm1") == DomainState.SHUT_OFF
    drv.start_domain(URI, "vm1")
    assert drv.list_domains(URI) == {"vm1": DomainState.RUNNING}
    drv.destroy_domain(URI, "vm1")
    drv.undefine_domain(URI, "vm1")
    assert drv.list_domains(URI) == {}


def test_not_idempotent_like_libvirt(drv):
    drv.define_domain(URI, SPEC)
    with pytest.raises(DomainExists):
        drv.define_domain(URI, SPEC)
    drv.start_domain(URI, "vm1")
    with pytest.raises(DriverError):
        drv.start_domain(URI, "vm1")
    with pytest.raises(DriverError):
        drv.undefine_domain(URI, "vm1")
    with pytest.raises(DomainNotFound):
        drv.start_domain(URI, "ghost")


def test_disks(drv):
    assert not drv.disk_exists("/srv/vm1.qcow2")
    drv.create_disk_overlay("/img/base.qcow2", "/srv/vm1.qcow2")
    assert drv.disk_exists("/srv/vm1.qcow2")
    with pytest.raises(DriverError):
        drv.create_disk_overlay("/img/base.qcow2", "/srv/vm1.qcow2")
    drv.delete_disk("/srv/vm1.qcow2")
    assert not drv.disk_exists("/srv/vm1.qcow2")


def test_injected_failures_count_down(drv):
    drv.fail["define_domain"] = 2
    for _ in range(2):
        with pytest.raises(DriverError, match="injected"):
            drv.define_domain(URI, SPEC)
    drv.define_domain(URI, SPEC)
    assert drv.domain_state(URI, "vm1") == DomainState.SHUT_OFF


def test_delay_before_hides_effect_delay_after_shows_it(drv):
    drv.delays["define_domain"] = 0.5
    th = threading.Thread(target=drv.define_domain, args=(URI, SPEC))
    th.start()
    time.sleep(0.2)
    assert drv.domain_state(URI, "vm1") is None  # not yet applied; reads are not blocked
    th.join()
    drv.delays.clear()
    drv.delays_after["start_domain"] = 0.5
    th = threading.Thread(target=drv.start_domain, args=(URI, "vm1"))
    th.start()
    time.sleep(0.2)
    assert drv.domain_state(URI, "vm1") == DomainState.RUNNING  # applied, call still sleeping
    th.join()


def test_state_file_shared_between_instances(tmp_path):
    f = tmp_path / "fake.json"
    a = FakeDriver(state_file=f)
    a.add_host(URI, 8, 16384)
    a.define_domain(URI, SPEC)
    b = FakeDriver(state_file=f)
    assert b.domain_state(URI, "vm1") == DomainState.SHUT_OFF
    b.start_domain(URI, "vm1")
    assert a.domain_state(URI, "vm1") == DomainState.RUNNING


def test_build_driver(tmp_path):
    d = build_driver(Settings(fake_state_file=tmp_path / "s.json", fake_fail={"start_domain": 1}))
    assert isinstance(d, FakeDriver) and d.fail == {"start_domain": 1}
    with pytest.raises(NotImplementedError):
        build_driver(Settings(driver="libvirt"))
