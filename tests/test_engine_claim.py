import threading

from sqlalchemy import select

from minihci.models import Task
from minihci.tasks.engine import TaskState, claim_next, enqueue


def test_enqueue_creates_pending(session_factory):
    with session_factory() as s:
        t = enqueue(s, "test.noop", target_id=7, params={"x": 1})
        assert t.id is not None
        s.commit()
    assert t.state == TaskState.PENDING and t.params == {"x": 1} and t.target_id == 7


def test_claim_marks_running_and_counts_attempt(session_factory):
    with session_factory() as s:
        enqueue(s, "test.noop", 1, {})
        s.commit()
    t = claim_next(session_factory)
    assert t.state == TaskState.RUNNING and t.attempts == 1 and t.started_at is not None
    assert claim_next(session_factory) is None


def test_claim_is_fifo(session_factory):
    with session_factory() as s:
        ids = [enqueue(s, "test.noop", i, {}).id for i in range(3)]
        s.commit()
    assert [claim_next(session_factory).id for _ in range(3)] == ids


def test_claim_skips_row_locked_by_another_transaction(session_factory):
    with session_factory() as s:
        a = enqueue(s, "test.noop", 1, {})
        b = enqueue(s, "test.noop", 2, {})
        s.commit()
    with session_factory() as holder:
        holder.execute(select(Task).where(Task.id == a.id).with_for_update()).scalar_one()
        t = claim_next(session_factory)  # must not block on a
        assert t.id == b.id
        holder.rollback()


def test_concurrent_claims_never_double_claim(session_factory):
    with session_factory() as s:
        for i in range(40):
            enqueue(s, "test.noop", i, {})
        s.commit()
    claimed, lock = [], threading.Lock()

    def loop():
        while (t := claim_next(session_factory)) is not None:
            with lock:
                claimed.append(t.id)

    threads = [threading.Thread(target=loop) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(claimed) == list(range(1, 41))
