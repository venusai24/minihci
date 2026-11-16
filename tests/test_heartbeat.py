import logging
import threading
import time
from datetime import UTC, datetime, timedelta

from minihci.config import Settings
from minihci.driver.fake import FakeDriver
from minihci.ha import heartbeat
from minihci.ha.heartbeat import heartbeat_once
from minihci.models import Host
from tests.helpers import add_ready_host

U1 = "qemu+ssh://a/system"
U2 = "qemu+ssh://b/system"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
S = Settings(heartbeat_suspect_misses=3, heartbeat_dead_after_s=30, heartbeat_ping_timeout_s=0.5)


def get(session_factory, hid) -> Host:
    with session_factory() as s:
        h = s.get(Host, hid)
        if h.last_heartbeat is not None:  # naive-UTC column
            h.last_heartbeat = h.last_heartbeat.replace(tzinfo=UTC)
        s.expunge(h)
        return h


def test_off_to_suspect_dead_and_back(session_factory):
    d = FakeDriver()
    h = add_ready_host(session_factory, d, "a", U1)
    heartbeat_once(session_factory, d, S, now=T0)
    row = get(session_factory, h)
    assert (row.status, row.missed_heartbeats, row.last_heartbeat) == ("ready", 0, T0)
    d.power_off_host(U1)
    for i in (1, 2, 3):
        heartbeat_once(session_factory, d, S, now=T0 + timedelta(seconds=5 * i))
    row = get(session_factory, h)
    assert (row.status, row.missed_heartbeats, row.last_heartbeat) == ("suspect", 3, T0)
    heartbeat_once(session_factory, d, S, now=T0 + timedelta(seconds=31))
    assert get(session_factory, h).status == "dead"
    d.power_on_host(U1)
    heartbeat_once(session_factory, d, S, now=T0 + timedelta(seconds=40))
    row = get(session_factory, h)
    assert (row.status, row.missed_heartbeats, row.last_heartbeat) == ("ready", 0, T0 + timedelta(seconds=40))


def test_transition_logged(session_factory, caplog):
    d = FakeDriver()
    add_ready_host(session_factory, d, "a", U1)
    d.power_off_host(U1)
    with caplog.at_level(logging.INFO):
        for i in (1, 2, 3):
            heartbeat_once(session_factory, d, S, now=T0 + timedelta(seconds=i))
    ev = [r for r in caplog.records if getattr(r, "fields", {}).get("event") == "host_health_changed"]
    assert len(ev) == 1 and ev[0].fields["to"] == "suspect" and ev[0].fields["from"] == "ready"


def test_removing_and_registering_untouched(session_factory):
    d = FakeDriver()
    r = add_ready_host(session_factory, d, "a", U1)
    g = add_ready_host(session_factory, d, "b", U2)
    with session_factory() as s:
        s.get(Host, r).status = "removing"
        s.get(Host, g).status = "registering"
        s.commit()
    d.power_off_host(U1)
    d.power_off_host(U2)
    heartbeat_once(session_factory, d, S, now=T0)
    for h, st in ((r, "removing"), (g, "registering")):
        row = get(session_factory, h)
        assert (row.status, row.missed_heartbeats, row.last_heartbeat) == (st, 0, None)


def test_status_changed_during_ping_is_not_overwritten(session_factory):
    d = FakeDriver()
    h = add_ready_host(session_factory, d, "a", U1)
    real = d.ping

    def ping(uri, timeout=3):
        with session_factory() as s:
            s.get(Host, h).status = "removing"
            s.commit()
        return real(uri, timeout)

    d.ping = ping
    heartbeat_once(session_factory, d, S, now=T0)
    row = get(session_factory, h)
    assert (row.status, row.last_heartbeat) == ("removing", None)


def test_ping_exception_counts_as_miss(session_factory):
    d = FakeDriver()
    h = add_ready_host(session_factory, d, "a", U1)

    def boom(uri, timeout=3):
        raise RuntimeError("boom")

    d.ping = boom
    heartbeat_once(session_factory, d, S, now=T0)
    row = get(session_factory, h)
    assert row.missed_heartbeats == 1 and row.status == "ready"


def test_hung_ping_does_not_delay_others(session_factory):
    d = FakeDriver()
    h1 = add_ready_host(session_factory, d, "a", U1)
    h2 = add_ready_host(session_factory, d, "b", U2)
    real = d.ping
    release = threading.Event()

    def ping(uri, timeout=3):
        if uri == U1:
            release.wait(5)
            return True
        return real(uri, timeout)

    d.ping = ping
    s = Settings(heartbeat_ping_timeout_s=0.3)
    seen = []

    def watcher():
        # the healthy host must be updated well before the hung ping times out
        for _ in range(40):
            if get(session_factory, h2).last_heartbeat == T0:
                seen.append(time.monotonic())
                return
            time.sleep(0.01)

    t0 = time.monotonic()
    th = threading.Thread(target=watcher)
    th.start()
    heartbeat_once(session_factory, d, s, now=T0)
    elapsed = time.monotonic() - t0
    th.join()
    release.set()
    assert elapsed < 2
    assert seen and seen[0] - t0 < 0.3
    row = get(session_factory, h1)
    assert row.missed_heartbeats == 1 and row.last_heartbeat == T0  # baseline from first miss


def test_default_now(session_factory):
    d = FakeDriver()
    h = add_ready_host(session_factory, d, "a", U1)
    heartbeat_once(session_factory, d, S)
    assert abs((get(session_factory, h).last_heartbeat - datetime.now(UTC)).total_seconds()) < 30


def _run(monkeypatch, fn, secs):
    monkeypatch.setattr(heartbeat, "heartbeat_once", fn)
    stop = threading.Event()
    th = threading.Thread(target=heartbeat.run_forever, args=(stop, None, None, Settings(heartbeat_interval_s=0.05)))
    th.start()
    time.sleep(secs)
    t0 = time.monotonic()
    stop.set()
    th.join(1)
    assert not th.is_alive() and time.monotonic() - t0 < 1


def test_run_forever_loops_and_stops(monkeypatch):
    calls = []
    _run(monkeypatch, lambda *a, **k: calls.append(1), 0.4)
    assert len(calls) >= 2


def test_run_forever_survives_exception(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("x")

    _run(monkeypatch, boom, 0.3)
    assert len(calls) >= 2
