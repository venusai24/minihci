import threading
import time

from minihci import reconcile
from minihci.config import Settings


def _start(monkeypatch, fake_once, interval=0.05):
    monkeypatch.setattr(reconcile, "reconcile_once", fake_once)
    stop = threading.Event()
    th = threading.Thread(
        target=reconcile.run_forever, args=(stop, None, None, Settings(reconcile_interval_s=interval))
    )
    th.start()
    return stop, th


def test_default_interval():
    assert Settings().reconcile_interval_s == 30


def test_run_forever_loops_and_stops_promptly(monkeypatch):
    calls = []
    stop, th = _start(monkeypatch, lambda *a: calls.append(1))
    time.sleep(0.4)
    t0 = time.monotonic()
    stop.set()
    th.join(1)
    assert not th.is_alive() and time.monotonic() - t0 < 1
    assert len(calls) >= 2


def test_exception_does_not_kill_loop(monkeypatch, caplog):
    calls = []

    def boom(*a):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("pass failed")

    stop, th = _start(monkeypatch, boom)
    time.sleep(0.4)
    stop.set()
    th.join(1)
    assert len(calls) >= 2
    assert any(r.exc_info for r in caplog.records)
