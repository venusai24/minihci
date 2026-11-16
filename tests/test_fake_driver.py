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
    HostStats,
    HostUnreachable,
    MigrationResult,
)
from minihci.driver.fake import FAKE_DOWNTIME_MS, FakeDriver

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


def test_shutdown_domain(drv):
    drv.define_domain(URI, SPEC)
    drv.start_domain(URI, "vm1")
    drv.shutdown_domain(URI, "vm1")
    assert drv.domain_state(URI, "vm1") == DomainState.SHUT_OFF


def test_shutdown_domain_errors(drv):
    with pytest.raises(DomainNotFound):
        drv.shutdown_domain(URI, "nope")
    drv.define_domain(URI, SPEC)
    with pytest.raises(DriverError):
        drv.shutdown_domain(URI, "vm1")


def test_reboot_domain_errors(drv):
    with pytest.raises(DomainNotFound):
        drv.reboot_domain(URI, "nope")
    drv.define_domain(URI, SPEC)
    with pytest.raises(DriverError):
        drv.reboot_domain(URI, "vm1")


def test_boot_count_increments_on_start_and_reboot(drv):
    drv.define_domain(URI, SPEC)
    assert drv.boot_count(URI, "vm1") == 0
    drv.start_domain(URI, "vm1")
    assert drv.boot_count(URI, "vm1") == 1
    drv.reboot_domain(URI, "vm1")
    assert drv.boot_count(URI, "vm1") == 2
    assert drv.domain_state(URI, "vm1") == DomainState.RUNNING


def test_shutdown_and_reboot_use_call_hooks(drv):
    drv.define_domain(URI, SPEC)
    drv.start_domain(URI, "vm1")
    drv.fail["shutdown_domain"] = 1
    drv.fail["reboot_domain"] = 1
    with pytest.raises(DriverError, match="injected"):
        drv.shutdown_domain(URI, "vm1")
    with pytest.raises(DriverError, match="injected"):
        drv.reboot_domain(URI, "vm1")
    assert drv.domain_state(URI, "vm1") == DomainState.RUNNING


def test_host_stats_default_from_running_domains(drv):
    assert drv.host_stats(URI) == HostStats(cpu_pct=0.0, mem_used_mb=0)
    drv.define_domain(URI, SPEC)
    assert drv.host_stats(URI).mem_used_mb == 0
    drv.start_domain(URI, "vm1")
    assert drv.host_stats(URI) == HostStats(cpu_pct=0.0, mem_used_mb=512)


def test_set_load_round_trip(drv):
    drv.set_load(URI, 55.5, 2048)
    assert drv.host_stats(URI) == HostStats(cpu_pct=55.5, mem_used_mb=2048)


def test_set_load_without_mem_uses_running_domains(drv):
    drv.define_domain(URI, SPEC)
    drv.start_domain(URI, "vm1")
    drv.set_load(URI, 40)
    assert drv.host_stats(URI) == HostStats(cpu_pct=40.0, mem_used_mb=512)


def test_set_load_file_backed_shared(tmp_path):
    f = tmp_path / "state.json"
    a = FakeDriver(state_file=f)
    a.add_host(URI, cpu_total=8, mem_total_mb=16384)
    a.set_load(URI, 77, 1000)
    assert FakeDriver(state_file=f).host_stats(URI) == HostStats(cpu_pct=77, mem_used_mb=1000)


def test_host_stats_unknown_uri(drv):
    with pytest.raises(HostUnreachable):
        drv.host_stats("qemu+ssh://nope/system")
    with pytest.raises(HostUnreachable):
        drv.set_load("qemu+ssh://nope/system", 10)


def test_host_stats_injected_failure(drv):
    drv.fail["host_stats"] = 1
    with pytest.raises(DriverError):
        drv.host_stats(URI)
    assert drv.host_stats(URI).cpu_pct == 0.0


@pytest.mark.parametrize("bad", [-0.1, 100.1])
def test_set_load_out_of_range(drv, bad):
    with pytest.raises(ValueError):
        drv.set_load(URI, bad)


# ---- migrate_live ------------------------------------------------------
URI2 = "qemu+ssh://b/system"


@pytest.fixture
def two(drv):
    drv.add_host(URI2, cpu_total=8, mem_total_mb=16384)
    return drv


def _running(d, uri=URI):
    d.define_domain(uri, SPEC)
    d.start_domain(uri, "vm1")


def test_migrate_live_happy_path(two):
    _running(two)
    res = two.migrate_live(URI, URI2, "vm1")
    assert isinstance(res, MigrationResult)
    assert res.duration_ms >= 0
    assert res.downtime_ms == FAKE_DOWNTIME_MS == 30
    assert two.domain_state(URI, "vm1") is None
    assert two.domain_state(URI2, "vm1") == DomainState.RUNNING
    assert two.boot_count(URI2, "vm1") == 1


def test_migrate_live_not_on_src(two):
    with pytest.raises(DomainNotFound):
        two.migrate_live(URI, URI2, "vm1")
    assert two.list_domains(URI2) == {}


def test_migrate_live_not_running(two):
    two.define_domain(URI, SPEC)
    with pytest.raises(DriverError):
        two.migrate_live(URI, URI2, "vm1")
    assert two.domain_state(URI, "vm1") == DomainState.SHUT_OFF
    assert two.list_domains(URI2) == {}


def test_migrate_live_name_on_dst(two):
    _running(two)
    two.define_domain(URI2, SPEC)
    with pytest.raises(DriverError):
        two.migrate_live(URI, URI2, "vm1")
    assert two.domain_state(URI, "vm1") == DomainState.RUNNING
    assert two.domain_state(URI2, "vm1") == DomainState.SHUT_OFF


@pytest.mark.parametrize("src,dst", [("qemu+ssh://x/system", URI2), (URI, "qemu+ssh://x/system")])
def test_migrate_live_unknown_host(two, src, dst):
    if src == URI:
        _running(two)
    with pytest.raises(HostUnreachable):
        two.migrate_live(src, dst, "vm1")
    if src == URI:
        assert two.domain_state(URI, "vm1") == DomainState.RUNNING


def test_migrate_live_injected_failure(two):
    two.fail = {"migrate_live": 1}
    _running(two)
    with pytest.raises(DriverError):
        two.migrate_live(URI, URI2, "vm1")
    assert two.domain_state(URI, "vm1") == DomainState.RUNNING
    assert two.list_domains(URI2) == {}
    two.migrate_live(URI, URI2, "vm1")
    assert two.domain_state(URI2, "vm1") == DomainState.RUNNING


def test_migrate_live_file_backed_visible_to_second_driver(tmp_path):
    f = tmp_path / "s.json"
    a, b = FakeDriver(state_file=f), FakeDriver(state_file=f)
    a.add_host(URI, 8, 16384)
    a.add_host(URI2, 8, 16384)
    _running(a)
    a.migrate_live(URI, URI2, "vm1")
    assert b.domain_state(URI, "vm1") is None
    assert b.domain_state(URI2, "vm1") == DomainState.RUNNING
