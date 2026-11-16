import threading
import time

import pytest
from sqlalchemy import select

from minihci.config import Settings
from minihci.driver.fake import FakeDriver
from minihci.models import Migration, Vm
from minihci.scheduler import rebalancer
from minihci.scheduler.rebalancer import rebalance_once
from tests.helpers import add_ready_host, drain
from tests.test_rebalancer import URI_B, S, configure, hot
from tests.test_vm_power import make_vm


@pytest.fixture
def driver():
    return FakeDriver()


def test_default_interval():
    assert Settings().rebalance_interval_s == 30


def test_end_to_end_migrates_to_other_host(session_factory, driver):
    ha = add_ready_host(session_factory, driver)
    hb = add_ready_host(session_factory, driver, name="b", uri=URI_B)
    configure(session_factory, rebalance_enabled=True)
    make_vm(session_factory, driver, ha, name="v1")
    make_vm(session_factory, driver, ha, name="v2")
    hot(session_factory, driver)
    move = rebalance_once(session_factory, S)
    assert move is not None
    drain(session_factory, driver)
    with session_factory() as s:
        vm = s.get(Vm, move.vm_id)
        assert vm.host_id == hb
        (m,) = s.scalars(select(Migration)).all()
        assert m.status == "succeeded" and m.downtime_ms is not None and m.vm_id == move.vm_id


def _start(monkeypatch, fake_once, interval=0.05):
    monkeypatch.setattr(rebalancer, "rebalance_once", fake_once)
    stop = threading.Event()
    th = threading.Thread(target=rebalancer.run_forever, args=(stop, None, Settings(rebalance_interval_s=interval)))
    th.start()
    return stop, th


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
