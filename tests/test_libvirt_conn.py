import threading
import time

import pytest

from minihci.driver import libvirt as lv

U1, U2 = "qemu+ssh://root@host1/system", "qemu+ssh://root@host2/system"


@pytest.fixture
def mgr(stub_libvirt):
    closed = []
    m = lv.LibvirtConnections(on_close=closed.append)
    m.closed = closed
    yield m
    lv.stop_event_loop()


def test_keepalive_before_event_loop_raises(stub_libvirt):
    c = stub_libvirt.open(U1)
    with pytest.raises(stub_libvirt.libvirtError):
        c.setKeepAlive(5, 3)


def test_start_event_loop_idempotent(stub_libvirt):
    try:
        t1 = lv.start_event_loop()
        t2 = lv.start_event_loop()
        assert t1 is t2 and t1.daemon and t1.is_alive()
        assert stub_libvirt.event_loop_registered
        time.sleep(0.1)
        assert stub_libvirt.event_loop_runs > 0
    finally:
        lv.stop_event_loop()


def test_get_conn_sets_keepalive_after_loop_and_caches(stub_libvirt, mgr):
    c = mgr.get(U1)
    assert c.keepalive == (5, 3)
    assert mgr.get(U1) is c
    assert stub_libvirt.open_calls == [U1]
    assert mgr.get(U2) is not c


def test_reconnect_when_not_alive(stub_libvirt, mgr):
    c = mgr.get(U1)
    c.alive = False
    c2 = mgr.get(U1)
    assert c2 is not c and c2.keepalive == (5, 3)


def test_close_callback_evicts_and_calls_hook(stub_libvirt, mgr):
    c = mgr.get(U1)
    c.simulate_close()
    assert mgr.closed == [U1]
    assert U1 not in mgr.cached_uris()
    assert mgr.get(U1) is not c


def test_stale_close_callback_does_not_evict_new_conn(stub_libvirt, mgr):
    c = mgr.get(U1)
    c.alive = False
    c2 = mgr.get(U1)
    c.simulate_close()
    assert mgr.get(U1) is c2


def test_drop_on_error(stub_libvirt, mgr):
    c = mgr.get(U1)
    mgr.drop(U1)
    assert c.closed and U1 not in mgr.cached_uris()


def test_ping_ok_and_down(stub_libvirt, mgr):
    assert mgr.ping(U1) is True
    stub_libvirt.host(U1).down = True
    assert mgr.ping(U1) is False
    assert U1 not in mgr.cached_uris()


def test_ping_hung_open_times_out(stub_libvirt, mgr):
    ev = stub_libvirt.hang(U1)
    t0 = time.monotonic()
    assert mgr.ping(U1, timeout=0.3) is False
    assert 0.25 < time.monotonic() - t0 < 1.5
    ev.set()


def test_second_ping_while_first_hangs_no_new_thread(stub_libvirt, mgr):
    ev = stub_libvirt.hang(U1)
    assert mgr.ping(U1, timeout=0.2) is False
    n = threading.active_count()
    t0 = time.monotonic()
    assert mgr.ping(U1, timeout=2) is False
    assert time.monotonic() - t0 < 0.5
    assert threading.active_count() == n
    assert stub_libvirt.open_calls == [U1]
    ev.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not mgr.ping(U1, timeout=1):
        time.sleep(0.05)
    assert mgr.ping(U1) is True


def test_ping_other_host_unaffected(stub_libvirt, mgr):
    ev = stub_libvirt.hang(U1)
    assert mgr.ping(U1, timeout=0.1) is False
    assert mgr.ping(U2) is True
    ev.set()


def test_close_unregisters_callback_first(stub_libvirt, mgr):
    c = mgr.get(U1)
    mgr.drop(U1)
    assert c.closed is True
    assert c.closed_with_cb is False
    assert c.close_cbs == []


def test_close_callback_defers_close(stub_libvirt, mgr):
    a = mgr.get(U1)
    a.simulate_close()
    assert a.closed is False
    assert U1 not in mgr.cached_uris()
    b = mgr.get(U1)
    assert b is not a
    assert a.closed is True
    assert a.closed_with_cb is False


def test_ping_failure_does_not_evict_newer_conn(stub_libvirt, mgr):
    a = mgr.get(U1)
    newer = []

    def swap(self_):
        b = stub_libvirt.open(U1)
        newer.append(b)
        self_.simulate_close()
        mgr._conns[U1] = b
        raise stub_libvirt.libvirtError("boom")

    a.getLibVersion = lambda: swap(a)
    assert mgr.ping(U1) is False
    b = newer[0]
    assert mgr.get(U1) is b
    assert b.closed is False
    assert a.closed is True
    assert a.close_count == 1


def test_close_callback_then_failed_ping_closes_once(stub_libvirt, mgr):
    a = mgr.get(U1)

    def boom():
        a.simulate_close()
        raise stub_libvirt.libvirtError("boom")

    a.getLibVersion = boom
    assert mgr.ping(U1) is False
    mgr.drop(U1)
    assert a.close_count == 1
    assert mgr.closed == [U1]


def test_drop_fires_on_close_hook_once_per_removed_conn(stub_libvirt, mgr):
    mgr.get(U1)
    mgr.drop(U1)
    assert mgr.closed == [U1]
    assert U1 not in mgr.cached_uris()
    mgr.drop(U1)  # nothing cached: no hook
    assert mgr.closed == [U1]
