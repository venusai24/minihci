from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from minihci.api.deps import get_session
from minihci.models import Task

router = APIRouter()


def task_json(t: Task) -> dict[str, Any]:
    def iso(d):
        return d.isoformat() if d else None

    return {
        "id": t.id,
        "type": t.type,
        "target_id": t.target_id,
        "state": t.state,
        "steps": t.steps,
        "attempts": t.attempts,
        "params": t.params,
        "created_at": iso(t.created_at),
        "started_at": iso(t.started_at),
        "finished_at": iso(t.finished_at),
        "error": t.error,
    }


@router.get("/tasks")
def list_tasks(state: str | None = None, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    q = select(Task).order_by(Task.id)
    if state is not None:
        q = q.where(Task.state == state)
    return [task_json(t) for t in session.scalars(q)]


@router.get("/tasks/{task_id}")
def get_task(task_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    t = session.get(Task, task_id)
    if t is None:
        raise HTTPException(404, "task not found")
    return task_json(t)
