import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from minihci.config import Settings
from minihci.driver.fake import FakeDriver
from minihci.ha.failover import failover_once
from minihci.models import Host, Task, Vm
from minihci.tasks.engine import enqueue
from tests.helpers import add_ready_host
from tests.test_vm_power import make_vm

URI_B = "qemu+ssh://b/system"
S = Settings()
HB = datetime(2026, 1, 1, 12, 0, 0)  # naive UTC, as stored


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def ha(session_factory, driver):
    return add_ready_host(session_factory, driver)


def set_host(session_factory, hid, status, hb=HB):
    with session_factory() as s, s.begin():
        h = s.get(Host, hid)
        h.status, h.last_heartbeat = status, hb


def set_vm(session_factory, vid, **kw):
    with session_factory() as s, s.begin():
        v = s.get(Vm, vid)
        for k, val in kw.items():
            setattr(v, k, val)


def vm(session_factory, vid) -> Vm:
    with session_factory() as s:
        return s.get(Vm, vid)


def tasks(session_factory):
    with session_factory() as s:
        return list(s.scalars(select(Task).where(Task.type == "vm.ha_restart").order_by(Task.id)))


def test_dead_host_enqueues_once_per_vm(session_factory, driver, ha):
    add_ready_host(session_factory, driver, name="b", uri=URI_B)
    v1 = make_vm(session_factory, driver, ha, name="v1")
    v2 = make_vm(session_factory, driver, ha, name="v2")
    set_host(session_factory, ha, "dead")
    failover_once(session_factory, S)
    ts = tasks(session_factory)
    assert sorted(t.target_id for t in ts) == [v1, v2]
    for t in ts:
        assert t.state == "pending"
        assert t.params["dead_host_id"] == ha
        assert datetime.fromisoformat(t.params["failed_at"]) == HB.replace(tzinfo=UTC)
    failover_once(session_factory, S)
    assert len(tasks(session_factory)) == 2


def test_stopped_and_absent_skipped(session_factory, driver, ha):
    add_ready_host(session_factory, driver, name="b", uri=URI_B)
    make_vm(session_factory, driver, ha, name="s", running=False)
    a = make_vm(session_factory, driver, ha, name="a")
    set_vm(session_factory, a, desired_state="absent", actual_state="absent")
    set_host(session_factory, ha, "dead")
    failover_once(session_factory, S)
    assert tasks(session_factory) == []


def test_suspect_host_no_tasks(session_factory, driver, ha):
    add_ready_host(session_factory, driver, name="b", uri=URI_B)
    make_vm(session_factory, driver, ha)
    set_host(session_factory, ha, "suspect")
    failover_once(session_factory, S)
    assert tasks(session_factory) == []


def test_no_capacity_pending_then_capacity_enqueues(session_factory, driver, ha, caplog):
    v = make_vm(session_factory, driver, ha)
    set_host(session_factory, ha, "dead")
    caplog.set_level("WARNING")
    failover_once(session_factory, S)
    assert tasks(session_factory) == []
    assert vm(session_factory, v).actual_state == "pending_restart"
    assert "ha_pending_restart" in caplog.text
    add_ready_host(session_factory, driver, name="b", uri=URI_B)
    failover_once(session_factory, S)
    (t,) = tasks(session_factory)
    assert t.target_id == v


def test_pending_restart_with_host_no_longer_dead_not_enqueued_and_unchanged(session_factory, driver, ha):
    add_ready_host(session_factory, driver, name="b", uri=URI_B)
    v = make_vm(session_factory, driver, ha)
    set_vm(session_factory, v, actual_state="pending_restart")
    set_host(session_factory, ha, "ready")
    failover_once(session_factory, S)
    assert tasks(session_factory) == []
    assert vm(session_factory, v).actual_state == "pending_restart"


def test_locked_vm_on_ready_host_skipped_fast(session_factory, driver, ha):
    v = make_vm(session_factory, driver, ha)
    with session_factory() as s2, s2.begin():
        s2.scalars(select(Vm).where(Vm.id == v).with_for_update()).first()
        t0 = time.monotonic()
        failover_once(session_factory, S)
        assert time.monotonic() - t0 < 1
    assert tasks(session_factory) == []
    assert vm(session_factory, v).actual_state == "running"


def test_locked_vm_on_dead_host_retried_next_pass(session_factory, driver, ha):
    add_ready_host(session_factory, driver, name="b", uri=URI_B)
    v = make_vm(session_factory, driver, ha)
    set_host(session_factory, ha, "dead")
    with session_factory() as s2, s2.begin():
        s2.scalars(select(Vm).where(Vm.id == v).with_for_update()).first()
        failover_once(session_factory, S)
        assert tasks(session_factory) == []
    failover_once(session_factory, S)
    assert [t.target_id for t in tasks(session_factory)] == [v]


def test_open_vm_task_blocks_ha_restart(session_factory, driver, ha):
    add_ready_host(session_factory, driver, name="b", uri=URI_B)
    v = make_vm(session_factory, driver, ha)
    set_host(session_factory, ha, "dead")
    with session_factory() as s, s.begin():
        mt = enqueue(s, "vm.migrate", v, {})
        mt.state = "running"
        mid = mt.id
    failover_once(session_factory, S)
    assert tasks(session_factory) == []
    assert vm(session_factory, v).actual_state == "running"
    with session_factory() as s, s.begin():
        s.get(Task, mid).state = "succeeded"
    failover_once(session_factory, S)
    assert len(tasks(session_factory)) == 1


def test_null_heartbeat_failed_at_fallback(session_factory, driver, ha):
    add_ready_host(session_factory, driver, name="b", uri=URI_B)
    make_vm(session_factory, driver, ha)
    set_host(session_factory, ha, "dead", hb=None)
    failover_once(session_factory, S)
    (t,) = tasks(session_factory)
    failed_at = datetime.fromisoformat(t.params["failed_at"])
    assert failed_at <= datetime.now(UTC) - timedelta(seconds=S.heartbeat_dead_after_s)


def test_running_vm_on_live_host_untouched(session_factory, driver, ha):
    v = make_vm(session_factory, driver, ha)
    failover_once(session_factory, S)
    assert tasks(session_factory) == []
    assert vm(session_factory, v).actual_state == "running"


def test_open_task_blocks_but_finished_does_not(session_factory, driver, ha):
    add_ready_host(session_factory, driver, name="b", uri=URI_B)
    make_vm(session_factory, driver, ha)
    set_host(session_factory, ha, "dead")
    failover_once(session_factory, S)
    (t,) = tasks(session_factory)
    with session_factory() as s, s.begin():
        s.get(Task, t.id).state = "failed"
    failover_once(session_factory, S)
    assert len(tasks(session_factory)) == 2


def test_heartbeat_run_forever_calls_failover(monkeypatch):
    import threading
    import time

    from minihci.ha import heartbeat

    calls = []
    monkeypatch.setattr(heartbeat, "heartbeat_once", lambda *a, **k: None)
    monkeypatch.setattr(heartbeat, "failover_once", lambda *a, **k: calls.append(1))
    stop = threading.Event()
    th = threading.Thread(target=heartbeat.run_forever, args=(stop, None, None, Settings(heartbeat_interval_s=0.05)))
    th.start()
    time.sleep(0.3)
    stop.set()
    th.join(1)
    assert len(calls) >= 2
