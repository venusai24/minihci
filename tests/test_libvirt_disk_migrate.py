import logging

import pytest

from minihci.config import Settings
from minihci.driver import build_driver
from minihci.driver import libvirt as lv
from minihci.driver.base import DomainSpec, DriverError, HostDriver, HostUnreachable

U1 = "qemu+ssh://root@host1/system"
U2 = "qemu+ssh://root@host2/system"
IMG, VMS = "/srv/minihci/base", "/srv/minihci/images"
BASE = f"{IMG}/base.qcow2"
DISK = f"{VMS}/vm1.qcow2"


@pytest.fixture
def drv(stub_libvirt):
    d = lv.LibvirtDriver(sleep=lambda s: None, storage_uris=[U1, U2], image_root=IMG, storage_root=VMS)
    shared = {}
    for u in (U1, U2):
        stub_libvirt.host(u).storage = shared  # shared NFS
    from tests.stubs.fake_libvirt import FakePool, FakeVolume

    pool = FakePool(stub_libvirt.host(U1), "seed", IMG)
    shared[BASE] = FakeVolume(pool, "base.qcow2", BASE, 10_000, None, "qcow2")
    d.lib = stub_libvirt
    yield d
    lv.stop_event_loop()


def test_isinstance_hostdriver(stub_libvirt):
    assert isinstance(lv.LibvirtDriver(), HostDriver)


def test_build_driver_selection(stub_libvirt):
    d = build_driver(Settings(driver="libvirt", libvirt_storage_uris=[U1]))
    assert isinstance(d, lv.LibvirtDriver)


def test_overlay_create_exists_delete_and_refresh(drv, stub_libvirt):
    assert not drv.disk_exists(DISK)
    drv.create_disk_overlay(BASE, DISK)
    h = stub_libvirt.host(U1)
    assert set(h.pools) == {"minihci-images", "minihci-vms"}
    assert all(p.active and p.autostart_ for p in h.pools.values())
    vol = h.storage[DISK]
    assert vol.capacity == 10_000 and vol.backing == BASE and vol.backing_format == "qcow2"
    assert drv.disk_exists(DISK)
    with pytest.raises(DriverError):
        drv.create_disk_overlay(BASE, DISK)
    before = h.pools["minihci-vms"].refresh_calls
    drv.delete_disk(DISK)
    assert h.pools["minihci-vms"].refresh_calls > before
    assert not drv.disk_exists(DISK)
    with pytest.raises(DriverError):
        drv.delete_disk(DISK)


def test_cross_host_volume_visible_only_after_refresh(drv, stub_libvirt):
    drv.create_disk_overlay(BASE, DISK)
    # another host (e.g. migration target) created a volume behind our back
    from tests.stubs.fake_libvirt import FakeVolume

    h1 = stub_libvirt.host(U1)
    other = f"{VMS}/other.qcow2"
    h1.storage[other] = FakeVolume(h1.pools["minihci-vms"], "other.qcow2", other, 1)
    assert h1.pools["minihci-vms"].visible.get(other) is None  # stale cache
    assert drv.disk_exists(other)  # refresh makes it visible


def test_disk_host_falls_back_and_unreachable(drv, stub_libvirt):
    stub_libvirt.host(U1).down = True
    drv.create_disk_overlay(BASE, DISK)
    assert stub_libvirt.host(U2).pools
    stub_libvirt.host(U2).down = True
    with pytest.raises(HostUnreachable):
        drv.disk_exists(DISK)


def test_overlay_rejects_base_outside_image_root(drv):
    with pytest.raises(DriverError):
        drv.create_disk_overlay("/etc/passwd", DISK)
    with pytest.raises(DriverError):
        drv.create_disk_overlay(f"{IMG}/../x", DISK)


def test_overlay_missing_base(drv):
    with pytest.raises(DriverError):
        drv.create_disk_overlay(f"{IMG}/nope.qcow2", DISK)


def _running_vm(drv, stub_libvirt):
    drv.define_domain(U1, DomainSpec("vm1", 1, 256, DISK))
    drv.start_domain(U1, "vm1")


def test_migrate_ok_flags_and_dest_stats(drv, stub_libvirt):
    _running_vm(drv, stub_libvirt)
    res = drv.migrate_live(U1, U2, "vm1")
    assert (res.duration_ms, res.downtime_ms) == (400, 90)
    _, _name, _, flags = stub_libvirt.host(U1).migrations[0]
    assert flags == 1 | 8 | 16 and not flags & 2
    assert "vm1" in stub_libvirt.host(U2).domains and "vm1" not in stub_libvirt.host(U1).domains


def test_migrate_missing_key_is_none_with_warning(drv, stub_libvirt, caplog):
    _running_vm(drv, stub_libvirt)
    stub_libvirt.host(U1).domains["vm1"].job_stats = {"time_elapsed": 400}
    with caplog.at_level(logging.WARNING):
        res = drv.migrate_live(U1, U2, "vm1")
    assert res.downtime_ms is None and res.duration_ms == 400
    assert "downtime" in caplog.text


def test_migrate_error_keeps_source(drv, stub_libvirt):
    _running_vm(drv, stub_libvirt)
    h2 = stub_libvirt.host(U2)
    from tests.stubs.fake_libvirt import _err

    stub_libvirt.host(U1).fail["migrate"] = _err("VIR_ERR_OPERATION_FAILED", "boom")
    with pytest.raises(DriverError):
        drv.migrate_live(U1, U2, "vm1")
    assert "vm1" in stub_libvirt.host(U1).domains and "vm1" not in h2.domains
    assert drv.domain_state(U1, "vm1").value == "running"


def test_migrate_not_running_and_dst_exists(drv, stub_libvirt):
    drv.define_domain(U1, DomainSpec("vm1", 1, 256, DISK))
    with pytest.raises(DriverError):
        drv.migrate_live(U1, U2, "vm1")
    drv.start_domain(U1, "vm1")
    drv.define_domain(U2, DomainSpec("vm1", 1, 256, DISK))
    with pytest.raises(DriverError):
        drv.migrate_live(U1, U2, "vm1")
    assert "vm1" in stub_libvirt.host(U1).domains


# ---- Q10.7 fix round ----


def _pool(stub_libvirt, name, path):
    from tests.stubs.fake_libvirt import FakePool

    h = stub_libvirt.host(U1)
    h.pools[name] = FakePool(h, name, path)
    h.fail["vol_create"] = AssertionError("createXML must not be called")
    return h


@pytest.mark.parametrize("name", ["minihci-vms", "minihci-images"])
def test_preexisting_pool_with_wrong_path_rejected(drv, stub_libvirt, name):
    _pool(stub_libvirt, name, "/other/dir")
    drv._storage_uris = [U1]  # with a fallback host the next one would be tried instead
    with pytest.raises(DriverError, match=f"pool {name} targets /other/dir, expected"):
        drv.create_disk_overlay(BASE, DISK)
    assert stub_libvirt.host(U1).pools[name].path == "/other/dir"  # never redefined


def test_pool_path_trailing_slash_passes(drv, stub_libvirt):
    h = _pool(stub_libvirt, "minihci-vms", VMS + "/")
    del h.fail["vol_create"]
    drv.create_disk_overlay(BASE, DISK)
    assert drv.disk_exists(DISK)


def test_delete_disk_refuses_path_outside_storage_root(drv, stub_libvirt):
    h = stub_libvirt.host(U1)
    h.fail["vol_delete"] = AssertionError("vol.delete must not be called")
    with pytest.raises(DriverError):
        drv.delete_disk(BASE)
    assert BASE in h.storage


def _kill_on_migrate(stub_libvirt, drv_conns, kill):
    """Make migrate3 fail with a connection error after killing the named hosts' connections."""
    from tests.stubs.fake_libvirt import _err

    dom = stub_libvirt.host(U1).domains["vm1"]

    def boom(dst_conn, params, flags):
        for u in kill:
            drv_conns[u].alive = False
        raise _err("VIR_ERR_SYSTEM_ERROR", "connection reset")

    dom.migrate3 = boom


@pytest.mark.parametrize(
    "kill,evicted",
    [([U1], {U1}), ([U2], {U2}), ([], {U1, U2})],
    ids=["src_dead", "dst_dead", "both_alive"],
)
def test_migrate_connection_error_evicts_dead_conns(drv, stub_libvirt, kill, evicted):
    _running_vm(drv, stub_libvirt)
    drv_conns = {u: drv._conns.get(u) for u in (U1, U2)}
    _kill_on_migrate(stub_libvirt, drv_conns, kill)
    with pytest.raises(HostUnreachable):
        drv.migrate_live(U1, U2, "vm1")
    for u in (U1, U2):
        cached = drv._conns._conns.get(u)
        assert (cached is None) == (u in evicted)
        assert drv_conns[u].closed == (u in evicted)


def test_migrate_dest_jobstats_failure_uses_wall_clock(drv, stub_libvirt, caplog, monkeypatch):
    from tests.stubs.fake_libvirt import _err

    _running_vm(drv, stub_libvirt)
    stub_libvirt.host(U1).domains["vm1"].job_stats = _err("VIR_ERR_OPERATION_INVALID", "no stats")
    ticks = iter([10.0, 11.5])
    monkeypatch.setattr(lv.time, "monotonic", lambda: next(ticks))
    with caplog.at_level(logging.WARNING):
        res = drv.migrate_live(U1, U2, "vm1")
    assert res.downtime_ms is None and res.duration_ms == 1500
    assert "vm1" in caplog.text


# ---- D-026 amended: storage hosts in order, fallback on any storage error, per-attempt timeout ----


def test_wrong_pool_on_first_storage_host_falls_back_to_the_next(drv, stub_libvirt):
    _pool(stub_libvirt, "minihci-vms", "/other/dir")
    drv.create_disk_overlay(BASE, DISK)  # U1's pool is wrong; U2 does it
    assert DISK in stub_libvirt.host(U2).storage
    assert stub_libvirt.host(U1).pools["minihci-vms"].path == "/other/dir"  # never redefined


def test_storage_error_other_than_unreachable_falls_back(drv, stub_libvirt):
    from tests.stubs.fake_libvirt import _err

    stub_libvirt.host(U1).fail["vol_create"] = _err("VIR_ERR_INTERNAL_ERROR", "no space left")
    drv.create_disk_overlay(BASE, DISK)
    assert drv.disk_exists(DISK)
    assert DISK in stub_libvirt.host(U2).storage


def test_all_storage_hosts_failing_raises_the_real_error_not_unreachable(drv, stub_libvirt):
    from tests.stubs.fake_libvirt import _err

    stub_libvirt.host(U1).fail["vol_create"] = _err("VIR_ERR_INTERNAL_ERROR", "no space left")
    stub_libvirt.host(U2).down = True
    with pytest.raises(DriverError, match="no space left") as ei:
        drv.create_disk_overlay(BASE, DISK)
    assert not isinstance(ei.value, HostUnreachable)


def test_precondition_failure_is_not_retried_on_the_next_host(drv, stub_libvirt):
    drv.create_disk_overlay(BASE, DISK)
    with pytest.raises(DriverError, match="already exists"):
        drv.create_disk_overlay(BASE, DISK)
    with pytest.raises(DriverError, match="not found"):
        drv.delete_disk(f"{VMS}/ghost.qcow2")
    assert not stub_libvirt.host(U2).conns  # U2 was never contacted


def test_hung_storage_host_times_out_and_falls_back(drv, stub_libvirt):
    import time

    drv._storage_timeout_s = 0.3
    ev = stub_libvirt.hang(U1)
    t0 = time.monotonic()
    try:
        drv.create_disk_overlay(BASE, DISK)
        assert time.monotonic() - t0 < 2
        assert DISK in stub_libvirt.host(U2).storage
    finally:
        ev.set()


def test_every_storage_host_hung_raises_unreachable(drv, stub_libvirt):
    drv._storage_timeout_s = 0.2
    evs = [stub_libvirt.hang(U1), stub_libvirt.hang(U2)]
    try:
        with pytest.raises(HostUnreachable, match="timed out"):
            drv.disk_exists(DISK)
    finally:
        for ev in evs:
            ev.set()


def test_no_storage_uris_configured_is_unreachable(stub_libvirt):
    d = lv.LibvirtDriver(storage_uris=[])
    with pytest.raises(HostUnreachable, match="no libvirt storage host"):
        d.disk_exists(DISK)


def test_default_storage_order_starts_with_the_local_nfs_server_and_has_a_timeout():
    s = Settings()
    assert s.libvirt_storage_uris[0] == "qemu:///system"
    assert s.libvirt_storage_timeout_s == 30


def test_build_driver_passes_timeout(stub_libvirt):
    d = build_driver(Settings(driver="libvirt", libvirt_storage_timeout_s=4.5))
    assert d._storage_timeout_s == 4.5 and d._storage_uris[0] == "qemu:///system"
