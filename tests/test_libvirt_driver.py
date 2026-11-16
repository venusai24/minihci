import pytest

from minihci.driver import libvirt as lv
from minihci.driver.base import (
    DomainExists,
    DomainNotFound,
    DomainSpec,
    DomainState,
    DriverError,
    HostUnreachable,
)

U1 = "qemu+ssh://root@host1/system"
SPEC = DomainSpec(name="vm1", vcpus=1, mem_mb=256, disk_path="/var/lib/minihci/vms/vm1.qcow2")


@pytest.fixture
def drv(stub_libvirt):
    sleeps = []
    d = lv.LibvirtDriver(sleep=sleeps.append)
    d.sleeps = sleeps
    yield d
    lv.stop_event_loop()


def test_probe_host(drv):
    info = drv.probe_host(U1)
    assert (info.cpu_total, info.mem_total_mb) == (2, 2456)


def test_probe_host_down(drv, stub_libvirt):
    stub_libvirt.host(U1).down = True
    with pytest.raises(HostUnreachable):
        drv.probe_host(U1)


def test_host_stats_first_call_two_samples(drv, stub_libvirt):
    h = stub_libvirt.host(U1)
    # make the second sample differ: +100 busy, +100 idle -> 50%
    orig = drv.sleeps.append

    def bump(s):
        orig(s)
        h.cpu = {**h.cpu, "user": h.cpu["user"] + 100, "idle": h.cpu["idle"] + 100}

    drv._sleep = bump
    st = drv.host_stats(U1)
    assert drv.sleeps == [0.5]
    assert st.cpu_pct == pytest.approx(50.0)
    assert st.mem_used_mb == (2_000_000 - 500_000 - 100_000 - 400_000) // 1024


def test_host_stats_second_call_uses_previous_sample(drv, stub_libvirt):
    h = stub_libvirt.host(U1)
    drv.host_stats(U1)
    h.cpu = {**h.cpu, "kernel": h.cpu["kernel"] + 300, "idle": h.cpu["idle"] + 100}
    st = drv.host_stats(U1)
    assert drv.sleeps == [0.5]  # no second sleep
    assert st.cpu_pct == pytest.approx(75.0)


def test_host_stats_down_unreachable(drv, stub_libvirt):
    stub_libvirt.host(U1).down = True
    with pytest.raises(HostUnreachable):
        drv.host_stats(U1)


def test_define_list_state_undefine(drv):
    drv.define_domain(U1, SPEC)
    assert drv.list_domains(U1) == {"vm1": DomainState.SHUT_OFF}
    assert drv.domain_state(U1, "vm1") == DomainState.SHUT_OFF
    drv.undefine_domain(U1, "vm1")
    assert drv.list_domains(U1) == {}
    assert drv.domain_state(U1, "vm1") is None


def test_define_twice_raises_exists(drv):
    drv.define_domain(U1, SPEC)
    with pytest.raises(DomainExists):
        drv.define_domain(U1, SPEC)


def test_start_stop_cycle(drv):
    drv.define_domain(U1, SPEC)
    drv.start_domain(U1, "vm1")
    assert drv.domain_state(U1, "vm1") == DomainState.RUNNING
    assert drv.list_domains(U1) == {"vm1": DomainState.RUNNING}
    drv.reboot_domain(U1, "vm1")
    drv.shutdown_domain(U1, "vm1")
    assert drv.domain_state(U1, "vm1") == DomainState.SHUT_OFF
    drv.start_domain(U1, "vm1")
    drv.destroy_domain(U1, "vm1")
    assert drv.domain_state(U1, "vm1") == DomainState.SHUT_OFF


def test_undefine_running_is_driver_error(drv):
    drv.define_domain(U1, SPEC)
    drv.start_domain(U1, "vm1")
    with pytest.raises(DriverError) as ei:
        drv.undefine_domain(U1, "vm1")
    assert not isinstance(ei.value, (DomainNotFound, HostUnreachable))
    assert drv.domain_state(U1, "vm1") == DomainState.RUNNING


def test_start_running_is_driver_error(drv):
    drv.define_domain(U1, SPEC)
    drv.start_domain(U1, "vm1")
    with pytest.raises(DriverError):
        drv.start_domain(U1, "vm1")


@pytest.mark.parametrize("op", ["destroy_domain", "shutdown_domain", "reboot_domain"])
def test_stop_ops_on_stopped_are_driver_error(drv, op):
    drv.define_domain(U1, SPEC)
    with pytest.raises(DriverError) as ei:
        getattr(drv, op)(U1, "vm1")
    assert type(ei.value) is DriverError


@pytest.mark.parametrize(
    "op", ["undefine_domain", "start_domain", "destroy_domain", "shutdown_domain", "reboot_domain"]
)
def test_missing_domain_not_found(drv, op):
    with pytest.raises(DomainNotFound):
        getattr(drv, op)(U1, "ghost")


def test_connection_loss_during_op_drops_conn_and_is_unreachable(drv, stub_libvirt):
    drv.define_domain(U1, SPEC)
    h = stub_libvirt.host(U1)
    conn = h.conns[0]
    h.fail["state"] = stub_libvirt.libvirtError("broken", stub_libvirt.VIR_ERR_RPC)
    with pytest.raises(HostUnreachable):
        drv.domain_state(U1, "vm1")
    assert conn.closed and U1 not in drv._conns.cached_uris()
    del h.fail["state"]
    assert drv.domain_state(U1, "vm1") == DomainState.SHUT_OFF  # reconnects


def test_start_has_no_timeout_waits_for_slow_create(drv, stub_libvirt):
    import threading
    import time

    drv.define_domain(U1, SPEC)
    ev = stub_libvirt.hang(U1, "create")
    t = threading.Timer(0.4, ev.set)
    t.start()
    t0 = time.monotonic()
    drv.start_domain(U1, "vm1")
    assert time.monotonic() - t0 >= 0.35
    assert drv.domain_state(U1, "vm1") == DomainState.RUNNING


def test_ping_delegates(drv, stub_libvirt):
    assert drv.ping(U1) is True


def _seed_sample(drv, stub_libvirt):
    drv.host_stats(U1)
    assert U1 in drv._cpu_prev


def test_op_error_does_not_evict_newer_conn(drv, stub_libvirt):
    drv.define_domain(U1, SPEC)
    _seed_sample(drv, stub_libvirt)
    a = drv._conns.get(U1)
    newer = []

    def swap(name):
        b = stub_libvirt.open(U1)
        newer.append(b)
        drv._conns._conns[U1] = b
        raise stub_libvirt.libvirtError("broken", stub_libvirt.VIR_ERR_RPC)

    a.lookupByName = swap
    with pytest.raises(HostUnreachable):
        drv.domain_state(U1, "vm1")
    b = newer[0]
    assert drv._conns.get(U1) is b
    assert b.closed is False
    assert a.closed is False
    assert U1 in drv._cpu_prev


def test_op_error_evicts_current_conn_and_forgets_sample(drv, stub_libvirt):
    drv.define_domain(U1, SPEC)
    _seed_sample(drv, stub_libvirt)
    h = stub_libvirt.host(U1)
    conn = h.conns[0]
    h.fail["state"] = stub_libvirt.libvirtError("broken", stub_libvirt.VIR_ERR_RPC)
    with pytest.raises(HostUnreachable):
        drv.domain_state(U1, "vm1")
    assert conn.closed and conn.close_count == 1
    assert U1 not in drv._conns.cached_uris()
    assert U1 not in drv._cpu_prev


def test_injected_hookless_connections_get_sample_hook(stub_libvirt):
    d = lv.LibvirtDriver(conns=lv.LibvirtConnections(), sleep=lambda s: None)
    try:
        d.define_domain(U1, SPEC)
        _seed_sample(d, stub_libvirt)
        h = stub_libvirt.host(U1)
        h.fail["state"] = stub_libvirt.libvirtError("broken", stub_libvirt.VIR_ERR_RPC)
        with pytest.raises(HostUnreachable):
            d.domain_state(U1, "vm1")
        assert U1 not in d._cpu_prev
    finally:
        lv.stop_event_loop()


def test_injected_caller_hook_not_overwritten(stub_libvirt):
    hook = lambda uri: None
    c = lv.LibvirtConnections(on_close=hook)
    lv.LibvirtDriver(conns=c)
    assert c._on_close is hook


def test_list_domains_skips_domain_vanishing_mid_listing(drv, stub_libvirt):
    drv.define_domain(U1, SPEC)
    drv.define_domain(U1, DomainSpec(name="vm2", vcpus=1, mem_mb=256, disk_path="/x/vm2.qcow2"))
    stub_libvirt.host(U1).domains["vm1"].defined = False
    assert drv.list_domains(U1) == {"vm2": DomainState.SHUT_OFF}
