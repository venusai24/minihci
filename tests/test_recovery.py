from sqlalchemy import func

from minihci.config import Settings
from minihci.driver.fake import FakeDriver
from minihci.models import Task
from minihci.tasks.recovery import RecoveryReport, recover_orphans
from tests.helpers import drain, load_task, make_three_step_type, step_rows

MID = step_rows(("a", "done"), ("b", "running"), ("c", "pending"))


def orphan(session_factory, attempts, steps=MID, type_="test.three", state="running") -> int:
    with session_factory() as s:
        t = Task(type=type_, target_id=1, params={}, state=state, attempts=attempts, steps=steps,
                 started_at=func.now())
        s.add(t)
        s.commit()
        return t.id


def test_orphan_below_max_attempts_is_requeued_then_resumes(session_factory):
    calls = []
    tt = make_three_step_type(calls)
    reg = {tt.name: tt}
    tid = orphan(session_factory, attempts=1)
    rep = recover_orphans(session_factory, FakeDriver(), Settings(max_attempts=3), registry=reg)
    assert rep == RecoveryReport(resumed=[tid], rolled_back=[], failed=[])
    t = load_task(session_factory, tid)
    assert t.state == "pending" and t.steps == MID
    drain(session_factory, FakeDriver(), registry=reg)
    assert calls == [("do", "b"), ("do", "c")]
    t = load_task(session_factory, tid)
    assert t.state == "succeeded" and t.attempts == 2


def test_orphan_at_max_attempts_is_rolled_back(session_factory):
    calls = []
    tt = make_three_step_type(calls)
    tid = orphan(session_factory, attempts=3)
    rep = recover_orphans(session_factory, FakeDriver(), Settings(max_attempts=3), registry={tt.name: tt})
    assert rep.rolled_back == [tid]
    assert calls == [("undo", "b"), ("undo", "a")]
    t = load_task(session_factory, tid)
    assert t.state == "rolled_back" and "max attempts" in t.error


def test_orphan_whose_rollback_fails_is_failed(session_factory):
    tt = make_three_step_type([], undo_fails=True)
    tid = orphan(session_factory, attempts=3)
    rep = recover_orphans(session_factory, FakeDriver(), Settings(max_attempts=3), registry={tt.name: tt})
    assert rep.failed == [tid] and load_task(session_factory, tid).state == "failed"


def test_orphan_with_unknown_type_is_failed(session_factory):
    tid = orphan(session_factory, attempts=1, type_="gone.type")
    rep = recover_orphans(session_factory, FakeDriver(), Settings(), registry={})
    assert rep.failed == [tid]
    assert "unknown task type: gone.type" in load_task(session_factory, tid).error


def test_non_running_tasks_untouched(session_factory):
    tt = make_three_step_type([])
    p = orphan(session_factory, attempts=0, steps=[], state="pending")
    d = orphan(session_factory, attempts=1, state="succeeded")
    rep = recover_orphans(session_factory, FakeDriver(), Settings(), registry={tt.name: tt})
    assert rep == RecoveryReport([], [], [])
    assert load_task(session_factory, p).state == "pending"
    assert load_task(session_factory, d).state == "succeeded"
