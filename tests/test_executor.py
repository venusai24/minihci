from minihci.config import Settings
from minihci.driver.fake import FakeDriver
from minihci.models import Task
from minihci.tasks.engine import TaskState, claim_next, enqueue
from minihci.tasks.executor import Step, TaskType, rollback_task, run_task
from tests.helpers import load_task, make_three_step_type, step_rows


def claimed(session_factory, type_="test.three") -> int:
    with session_factory() as s:
        enqueue(s, type_, 1, {})
        s.commit()
    return claim_next(session_factory).id


def run(session_factory, tt, task_id):
    return run_task(task_id, session_factory, FakeDriver(), Settings(), registry={tt.name: tt})


def statuses(t):
    return [(s["name"], s["status"]) for s in t.steps]


def test_success_records_every_step(session_factory):
    calls = []
    tt = make_three_step_type(calls)
    tid = claimed(session_factory)
    assert run(session_factory, tt, tid) == TaskState.SUCCEEDED
    t = load_task(session_factory, tid)
    assert t.state == "succeeded" and t.finished_at is not None and t.error is None
    assert statuses(t) == [("a", "done"), ("b", "done"), ("c", "done")]
    assert all(s["started_at"] and s["finished_at"] for s in t.steps)
    assert calls == [("do", "a"), ("do", "b"), ("do", "c")]


def test_step_committed_as_running_before_it_executes(session_factory):
    seen = {}

    def peek(ctx):
        with session_factory() as s:  # separate session: only sees committed data
            seen["steps"] = s.get(Task, ctx.task_id).steps

    tt = TaskType("test.peek", "vm", [Step("only", peek)])
    tid = claimed(session_factory, "test.peek")
    run(session_factory, tt, tid)
    assert seen["steps"][0]["status"] == "running"


def test_failure_rolls_back_in_reverse_including_failed_step(session_factory):
    calls = []
    tt = make_three_step_type(calls, fail_at="b")
    tid = claimed(session_factory)
    assert run(session_factory, tt, tid) == TaskState.ROLLED_BACK
    t = load_task(session_factory, tid)
    assert calls == [("do", "a"), ("do", "b"), ("undo", "b"), ("undo", "a")]
    assert statuses(t) == [("a", "undone"), ("b", "undone"), ("c", "pending")]
    assert "boom in b" in t.error and t.finished_at is not None


def test_all_undos_failing_marks_every_step_undo_failed(session_factory):
    calls = []
    tt = make_three_step_type(calls, fail_at="b", undo_fails=True)
    tid = claimed(session_factory)
    assert run(session_factory, tt, tid) == TaskState.FAILED
    t = load_task(session_factory, tid)
    assert calls == [("do", "a"), ("do", "b"), ("undo", "b"), ("undo", "a")]
    assert statuses(t) == [("a", "undo_failed"), ("b", "undo_failed"), ("c", "pending")]
    assert "boom in b" in t.error
    assert "undo of b failed: undo broke b" in t.error and "undo of a failed: undo broke a" in t.error


def test_rollback_continues_past_failing_undo(session_factory):
    calls, hits = [], []
    base = make_three_step_type(calls, fail_at="c", undo_fails={"b"})
    tt = TaskType(base.name, "vm", base.steps, on_rolled_back=lambda ctx: hits.append(1))
    tid = claimed(session_factory)
    assert run(session_factory, tt, tid) == TaskState.FAILED
    t = load_task(session_factory, tid)
    assert calls == [("do", "a"), ("do", "b"), ("do", "c"), ("undo", "c"), ("undo", "b"), ("undo", "a")]
    assert statuses(t) == [("a", "undone"), ("b", "undo_failed"), ("c", "undone")]
    assert hits == []
    assert t.error.startswith("step c failed: boom in c; undo of b failed: undo broke b")
    assert t.finished_at is not None


def test_resume_skips_done_steps_and_reruns_running_one(session_factory):
    calls = []
    tt = make_three_step_type(calls)
    tid = claimed(session_factory)
    with session_factory() as s:
        s.get(Task, tid).steps = step_rows(("a", "done"), ("b", "running"), ("c", "pending"))
        s.commit()
    assert run(session_factory, tt, tid) == TaskState.SUCCEEDED
    assert calls == [("do", "b"), ("do", "c")]


def test_on_rolled_back_hook_runs(session_factory):
    hits = []
    base = make_three_step_type([], fail_at="a")
    tt = TaskType(base.name, "vm", base.steps, on_rolled_back=lambda ctx: hits.append(ctx.target_id))
    tid = claimed(session_factory)
    assert run(session_factory, tt, tid) == TaskState.ROLLED_BACK
    assert hits == [1]


def test_rollback_task_directly(session_factory):
    calls = []
    tt = make_three_step_type(calls)
    tid = claimed(session_factory)
    with session_factory() as s:
        s.get(Task, tid).steps = step_rows(("a", "done"), ("b", "running"), ("c", "pending"))
        s.commit()
    state = rollback_task(tid, session_factory, FakeDriver(), Settings(), {tt.name: tt}, reason="max attempts exceeded")
    assert state == TaskState.ROLLED_BACK
    assert calls == [("undo", "b"), ("undo", "a")]
    assert "max attempts exceeded" in load_task(session_factory, tid).error


def test_unknown_type_fails(session_factory):
    tid = claimed(session_factory, "nope")
    assert run_task(tid, session_factory, FakeDriver(), Settings(), registry={}) == TaskState.FAILED
    assert "unknown task type: nope" in load_task(session_factory, tid).error
