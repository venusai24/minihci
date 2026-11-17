from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from minihci.config import Settings
from minihci.dr.scheduler import replicate_once, run_forever
from minihci.driver.fake import FakeDriver
from minihci.models import Host, ProtectionPolicy, Replica, Task, Vm
from minihci.tasks.engine import enqueue
from tests.helpers import add_ready_host
from tests.test_vm_power import make_vm

S = Settings(replication_retry_s=60)
NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def host(session_factory, driver):
    return add_ready_host(session_factory, driver)


def protected(session_factory, driver, host, name="v1", interval=60, enabled=True) -> int:
    vid = make_vm(session_factory, driver, host, name=name)
    with session_factory() as s, s.begin():
        s.add(ProtectionPolicy(vm_id=vid, interval_s=interval, retention=3, enabled=enabled))
    return vid


def add_task(session_factory, vid, type_, state, finished_ago=None, now=NOW) -> int:
    with session_factory() as s, s.begin():
        t = Task(type=type_, target_id=vid, state=state)
        if finished_ago is not None:
            # tasks.finished_at is a naive UTC timestamp
            t.finished_at = (now - timedelta(seconds=finished_ago)).replace(tzinfo=None)
        s.add(t)
        s.flush()
        return t.id


def add_replica(session_factory, vid, hid, age_s, now=NOW) -> None:
    tid = add_task(session_factory, vid, "vm.replicate", "succeeded", age_s, now)
    with session_factory() as s, s.begin():
        s.add(
            Replica(
                vm_id=vid,
                task_id=tid,
                host_id=hid,
                mode="full",
                status="succeeded",
                seq=0,
                snapshot_time=now - timedelta(seconds=age_s),
            )
        )


def pending(session_factory):
    with session_factory() as s:
        return list(
            s.scalars(select(Task).where(Task.type == "vm.replicate", Task.state == "pending").order_by(Task.id))
        )


def test_due_vm_enqueued_once(session_factory, driver, host):
    v = protected(session_factory, driver, host)
    first = replicate_once(session_factory, S, NOW)
    assert replicate_once(session_factory, S, NOW) == []
    ts = pending(session_factory)
    assert [t.target_id for t in ts] == [v]
    assert first == [ts[0].id]


def test_not_due_before_interval(session_factory, driver, host):
    v = protected(session_factory, driver, host, interval=60)
    add_replica(session_factory, v, host, 30)
    assert replicate_once(session_factory, S, NOW) == []
    assert pending(session_factory) == []
    later = NOW + timedelta(seconds=31)  # snapshot now 61 s old
    assert len(replicate_once(session_factory, S, later)) == 1


def test_skips_stopped_disabled_dr_site_and_busy(session_factory, driver, host):
    stopped = protected(session_factory, driver, host, name="stopped")
    disabled = protected(session_factory, driver, host, name="disabled", enabled=False)
    dr = protected(session_factory, driver, host, name="dr")
    busy = protected(session_factory, driver, host, name="busy")
    with session_factory() as s, s.begin():
        s.get(Vm, stopped).actual_state = "stopped"
    other = add_ready_host(session_factory, driver, name="b", uri="qemu+ssh://b/system")
    with session_factory() as s, s.begin():
        s.get(Host, other).site = "dr"
        s.get(Vm, dr).host_id = other
    add_task(session_factory, busy, "vm.stop", "running")
    assert disabled and replicate_once(session_factory, S, NOW) == []
    assert pending(session_factory) == []


def test_not_ready_host_skipped(session_factory, driver, host):
    protected(session_factory, driver, host)
    with session_factory() as s, s.begin():
        s.get(Host, host).status = "suspect"
    assert replicate_once(session_factory, S, NOW) == []


def test_desired_state_must_be_running(session_factory, driver, host):
    v = protected(session_factory, driver, host)
    with session_factory() as s, s.begin():
        s.get(Vm, v).desired_state = "stopped"
    assert replicate_once(session_factory, S, NOW) == []


def test_failed_replica_backs_off(session_factory, driver, host):
    v = protected(session_factory, driver, host)
    add_task(session_factory, v, "vm.replicate", "failed", 10)
    assert replicate_once(session_factory, S, NOW) == []
    assert len(replicate_once(session_factory, S, NOW + timedelta(seconds=51))) == 1  # failure now 61 s old


def test_rolled_back_also_backs_off(session_factory, driver, host):
    v = protected(session_factory, driver, host)
    add_task(session_factory, v, "vm.replicate", "rolled_back", 10)
    assert replicate_once(session_factory, S, NOW) == []


def test_backoff_is_per_vm(session_factory, driver, host):
    v1 = protected(session_factory, driver, host, name="v1")
    v2 = protected(session_factory, driver, host, name="v2")
    add_task(session_factory, v1, "vm.replicate", "failed", 10)
    ids = replicate_once(session_factory, S, NOW)
    assert len(ids) == 1
    assert [t.target_id for t in pending(session_factory)] == [v2]


def test_other_task_type_failure_does_not_back_off(session_factory, driver, host):
    v = protected(session_factory, driver, host)
    add_task(session_factory, v, "vm.start", "failed", 10)
    assert len(replicate_once(session_factory, S, NOW)) == 1


def test_now_defaults_to_current_time(session_factory, driver, host):
    v = protected(session_factory, driver, host)
    real = datetime.now(UTC)
    add_replica(session_factory, v, host, 30, now=real)
    assert replicate_once(session_factory, S) == []
    add_task(session_factory, v, "vm.replicate", "failed", 0, now=real)
    assert replicate_once(session_factory, S, real + timedelta(seconds=200)) != []


@pytest.mark.timeout(5)
def test_skip_locked_vm_row(session_factory, driver, host):
    v = protected(session_factory, driver, host)
    with session_factory() as s2, s2.begin():
        s2.scalars(select(Vm).where(Vm.id == v).with_for_update()).first()
        assert replicate_once(session_factory, S, NOW) == []
    assert len(replicate_once(session_factory, S, NOW)) == 1  # level-triggered: next pass enqueues


def test_open_task_rechecked_under_lock(session_factory, driver, host):
    v = protected(session_factory, driver, host)
    with session_factory() as s, s.begin():
        enqueue(s, "vm.replicate", v, {})
    assert replicate_once(session_factory, S, NOW) == []


def test_run_forever_survives_exception(session_factory, monkeypatch):
    import threading

    import minihci.dr.scheduler as sch

    stop = threading.Event()
    calls = []

    def boom(*a, **k):
        calls.append(1)
        if len(calls) >= 2:
            stop.set()
        raise RuntimeError("x")

    monkeypatch.setattr(sch, "replicate_once", boom)
    run_forever(stop, session_factory, Settings(replication_tick_s=0.01))
    assert len(calls) == 2


def test_exception_in_one_vm_does_not_starve_others(session_factory, driver, host, monkeypatch, caplog):
    import minihci.dr.scheduler as sch

    v1 = protected(session_factory, driver, host, name="v1")
    v2 = protected(session_factory, driver, host, name="v2")
    v3 = protected(session_factory, driver, host, name="v3")
    real = sch._handle

    def flaky(sf, vm_id, settings, now):
        if vm_id == v1:
            raise RuntimeError("boom")
        return real(sf, vm_id, settings, now)

    monkeypatch.setattr(sch, "_handle", flaky)
    ids = replicate_once(session_factory, Settings(replication_retry_s=60, worker_threads=4), NOW)  # cap 3
    assert len(ids) == 2
    assert sorted(t.target_id for t in pending(session_factory)) == [v2, v3]
    assert any(getattr(r, "fields", {}).get("vm_id") == v1 for r in caplog.records)


def test_missing_host_row_is_not_due(session_factory, driver, host, monkeypatch):
    from sqlalchemy.orm import Session

    protected(session_factory, driver, host)
    real_get = Session.get

    def get(self, entity, ident, *a, **k):
        return None if entity is Host else real_get(self, entity, ident, *a, **k)

    monkeypatch.setattr(Session, "get", get)
    assert replicate_once(session_factory, S, NOW) == []
    assert pending(session_factory) == []


def test_open_replicas_capped_below_worker_threads(session_factory, driver, host):
    """F4: a backup holds a worker thread (and the VM lock) for up to backup_timeout_s; keep at least one worker
    free for vm.ha_restart / vm.dr_failover. With worker_threads=2 only one vm.replicate may be open at a time."""
    s2 = Settings(replication_retry_s=60, worker_threads=2)
    vids = [protected(session_factory, driver, host, name=f"v{i}") for i in range(3)]
    first = replicate_once(session_factory, s2, NOW)
    assert len(first) == 1
    assert [t.target_id for t in pending(session_factory)] == [vids[0]]
    assert replicate_once(session_factory, s2, NOW) == []  # still open: the cap holds across passes
    with session_factory() as s, s.begin():
        t = s.get(Task, first[0])
        t.state = "running"
    assert replicate_once(session_factory, s2, NOW) == []  # running counts as open too
    with session_factory() as s, s.begin():
        s.get(Task, first[0]).state = "succeeded"
    add_replica(session_factory, vids[0], host, 0)  # what the finished task left behind: v0 is not due now
    second = replicate_once(session_factory, s2, NOW)  # level-triggered: the next due VM is picked up
    assert len(second) == 1
    assert [t.target_id for t in pending(session_factory)] == [vids[1]]


def test_open_replica_cap_scales_with_worker_threads(session_factory, driver, host):
    s4 = Settings(replication_retry_s=60, worker_threads=4)
    for i in range(5):
        protected(session_factory, driver, host, name=f"v{i}")
    assert len(replicate_once(session_factory, s4, NOW)) == 3
    s1 = Settings(replication_retry_s=60, worker_threads=1)
    assert replicate_once(session_factory, s1, NOW) == []  # max(1, 0) = 1 and 3 are already open
