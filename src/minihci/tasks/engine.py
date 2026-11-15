from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from minihci.models import Task


class TaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


TERMINAL_STATES = frozenset({TaskState.SUCCEEDED, TaskState.FAILED, TaskState.ROLLED_BACK})


def enqueue(session: Session, type: str, target_id: int, params: dict[str, Any]) -> Task:
    """Add a pending task and flush; the caller commits (same txn as the rows it targets)."""
    task = Task(type=type, target_id=target_id, params=params)
    session.add(task)
    session.flush()
    return task


def claim_next(session_factory: sessionmaker) -> Task | None:
    """Claim the oldest pending task in its own transaction; return it detached."""
    with session_factory() as s, s.begin():
        task = s.scalars(
            select(Task)
            .where(Task.state == TaskState.PENDING)
            .order_by(Task.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        ).first()
        if task is None:
            return None
        task.state = TaskState.RUNNING
        task.attempts += 1
        task.started_at = func.now()
        s.flush()
        s.refresh(task)
        s.expunge(task)
        return task
