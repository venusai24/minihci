import hashlib
import json
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from minihci.models import Task

MAX_KEY_LEN = 255
_KEY_CONSTRAINT = "uq_tasks_idempotency_key"


class IdempotencyConflict(Exception):
    """The Idempotency-Key was already used with a different request body."""

    def __init__(self, task_id: int) -> None:
        super().__init__(f"idempotency key already used by task {task_id} with a different request")
        self.task_id = task_id


def fingerprint(body: Mapping[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _existing(session_factory: sessionmaker, key: str, request_hash: str) -> tuple[Task, bool] | None:
    with session_factory() as s:
        original = s.scalars(select(Task).where(Task.idempotency_key == key)).first()
        if original is None:
            return None
        if original.request_hash != request_hash:
            raise IdempotencyConflict(original.id)
        s.expunge(original)
        return original, False


def run_idempotent(
    session_factory: sessionmaker,
    key: str | None,
    request_hash: str,
    create: Callable[[Session], Task],
) -> tuple[Task, bool]:
    """Run `create` at most once per key. Returns (task, created)."""
    if key is not None and not 1 <= len(key) <= MAX_KEY_LEN:
        raise ValueError(f"Idempotency-Key must be 1-{MAX_KEY_LEN} characters")
    if key is not None and (hit := _existing(session_factory, key, request_hash)) is not None:
        return hit
    with session_factory() as s:
        try:
            task = create(s)
            task.idempotency_key = key
            task.request_hash = request_hash
            s.commit()
            return task, True
        except IntegrityError as e:
            # Rolling back also discards create()'s side rows (e.g. the VM row).
            s.rollback()
            constraint = getattr(getattr(e.orig, "diag", None), "constraint_name", None)
            if key is None or constraint != _KEY_CONSTRAINT:
                raise
    hit = _existing(session_factory, key, request_hash)
    if hit is None:  # the winning txn vanished between the conflict and the lookup
        raise RuntimeError("idempotency conflict but no original task found")
    return hit
