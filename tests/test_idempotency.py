import threading

import pytest
from sqlalchemy import func, select

from minihci.api.idempotency import IdempotencyConflict, fingerprint, run_idempotent
from minihci.models import Task
from minihci.tasks.engine import enqueue


def creator(calls=None, barrier=None):
    def create(s):
        if calls is not None:
            calls.append(1)
        if barrier is not None:
            barrier.wait(timeout=5)
        return enqueue(s, "test.noop", 1, {})
    return create


def count(session_factory):
    with session_factory() as s:
        return s.scalar(select(func.count()).select_from(Task))


FP = fingerprint({"x": 1})


def test_no_key_always_creates(session_factory):
    a, ca = run_idempotent(session_factory, None, FP, creator())
    b, cb = run_idempotent(session_factory, None, FP, creator())
    assert ca and cb and a.id != b.id


def test_same_key_same_body_returns_original_without_side_effects(session_factory):
    calls = []
    a, ca = run_idempotent(session_factory, "k1", FP, creator(calls))
    b, cb = run_idempotent(session_factory, "k1", FP, creator(calls))
    assert ca and not cb and b.id == a.id
    assert calls == [1] and count(session_factory) == 1


def test_same_key_different_body_conflicts(session_factory):
    a, _ = run_idempotent(session_factory, "k1", fingerprint({"x": 1}), creator())
    with pytest.raises(IdempotencyConflict) as e:
        run_idempotent(session_factory, "k1", fingerprint({"x": 2}), creator())
    assert e.value.task_id == a.id


def test_concurrent_same_key_creates_one_task(session_factory):
    barrier = threading.Barrier(5)
    results, lock = [], threading.Lock()

    def go():
        r = run_idempotent(session_factory, "race", FP, creator(barrier=barrier))
        with lock:
            results.append(r)

    threads = [threading.Thread(target=go) for _ in range(5)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len({t.id for t, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    assert count(session_factory) == 1


def test_fingerprint_ignores_key_order():
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})


@pytest.mark.parametrize("key", ["", "x" * 256])
def test_key_length_validated(session_factory, key):
    with pytest.raises(ValueError):
        run_idempotent(session_factory, key, FP, creator())
