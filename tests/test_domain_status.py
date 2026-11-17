"""D-026 amendment (4): PAUSED and CRASHED domain states carry the libvirt reason."""

import pytest

from minihci.driver import libvirt as lv
from minihci.driver.base import DomainSpec, DomainState, DomainStatus, HostDriver
from minihci.driver.fake import FakeDriver
from minihci.driver.libvirt_stats import domain_reason, map_domain_state

U1 = "qemu+ssh://root@host1/system"
SPEC = DomainSpec(name="vm1", vcpus=1, mem_mb=256, disk_path="/var/lib/minihci/vms/vm1.qcow2")


@pytest.mark.parametrize(
    "code,want",
    [
        (0, DomainState.SHUT_OFF),
        (1, DomainState.RUNNING),
        (2, DomainState.RUNNING),
        (3, DomainState.PAUSED),
        (4, DomainState.RUNNING),
        (5, DomainState.SHUT_OFF),
        (6, DomainState.CRASHED),
        (7, DomainState.RUNNING),
    ],
)
def test_map_domain_state(code, want):
    assert map_domain_state(code) is want


@pytest.mark.parametrize(
    "code,reason,want",
    [
        (3, 1, "user"),
        (3, 2, "migration"),
        (3, 5, "ioerror"),
        (3, 6, "watchdog"),
        (3, 99, "unknown"),
        (6, 1, "panicked"),
        (6, 0, "unknown"),
        (5, 3, "crashed"),
        (5, 2, "destroyed"),
        (1, 1, None),  # running: no reason worth keeping
    ],
)
def test_domain_reason(code, reason, want):
    assert domain_reason(code, reason) == want


def test_active_states():
    assert [s for s in DomainState if s.active] == [DomainState.RUNNING, DomainState.PAUSED, DomainState.CRASHED]


def test_status_unhealthy_is_paused_with_ioerror_only():
    assert DomainStatus(DomainState.PAUSED, "ioerror").unhealthy
    assert not DomainStatus(DomainState.PAUSED, "user").unhealthy
    assert not DomainStatus(DomainState.RUNNING, None).unhealthy
    assert not DomainStatus(DomainState.CRASHED, "panicked").unhealthy


@pytest.fixture
def drv(stub_libvirt):
    d = lv.LibvirtDriver(sleep=lambda s: None)
    d.define_domain(U1, SPEC)
    yield d
    lv.stop_event_loop()


def _set(stub_libvirt, state, reason):
    dom = stub_libvirt.host(U1).domains["vm1"]
    dom.state_, dom.reason_ = state, reason


def test_libvirt_domain_status_paused_ioerror(drv, stub_libvirt):
    _set(stub_libvirt, 3, 5)
    assert drv.domain_status(U1, "vm1") == DomainStatus(DomainState.PAUSED, "ioerror")
    assert drv.domain_state(U1, "vm1") == DomainState.PAUSED
    assert drv.list_domains(U1) == {"vm1": DomainState.PAUSED}


def test_libvirt_domain_status_crashed_and_shutoff(drv, stub_libvirt):
    _set(stub_libvirt, 6, 1)
    assert drv.domain_status(U1, "vm1") == DomainStatus(DomainState.CRASHED, "panicked")
    _set(stub_libvirt, 5, 3)
    assert drv.domain_status(U1, "vm1") == DomainStatus(DomainState.SHUT_OFF, "crashed")


def test_libvirt_domain_status_missing_is_none(drv):
    assert drv.domain_status(U1, "ghost") is None


def test_libvirt_destroy_works_on_paused_domain(drv, stub_libvirt):
    _set(stub_libvirt, 3, 1)
    drv.destroy_domain(U1, "vm1")
    assert drv.domain_state(U1, "vm1") == DomainState.SHUT_OFF


def test_fake_driver_status_and_helper():
    d = FakeDriver()
    assert isinstance(d, HostDriver)
    d.add_host(U1, cpu_total=4, mem_total_mb=4096)
    d.define_domain(U1, SPEC)
    assert d.domain_status(U1, "vm1") == DomainStatus(DomainState.SHUT_OFF, None)
    d.set_domain_state(U1, "vm1", DomainState.PAUSED, "ioerror")
    assert d.domain_status(U1, "vm1") == DomainStatus(DomainState.PAUSED, "ioerror")
    assert d.domain_status(U1, "ghost") is None


def test_fake_driver_paused_is_active():
    d = FakeDriver()
    d.add_host(U1, cpu_total=4, mem_total_mb=4096)
    d.define_domain(U1, SPEC)
    d.set_domain_state(U1, "vm1", DomainState.PAUSED, "user")
    from minihci.driver.base import DriverError

    with pytest.raises(DriverError):
        d.undefine_domain(U1, "vm1")
    d.destroy_domain(U1, "vm1")
    assert d.domain_state(U1, "vm1") == DomainState.SHUT_OFF
