import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import func
from sqlalchemy.orm import sessionmaker

from minihci.config import Settings
from minihci.driver.base import HostDriver
from minihci.logging import task_context
from minihci.models import Task
from minihci.tasks.engine import TaskState

logger = logging.getLogger("minihci.tasks")


@dataclass
class StepContext:
    task_id: int
    target_id: int
    params: dict[str, Any]
    driver: HostDriver
    session_factory: sessionmaker
    settings: Settings
    log: logging.Logger


@dataclass(frozen=True)
class Step:
    name: str
    do: Callable[[StepContext], None]
    undo: Callable[[StepContext], None] | None = None


@dataclass(frozen=True)
class TaskType:
    name: str
    target_kind: Literal["vm", "host"]
    steps: list[Step]
    on_rolled_back: Callable[[StepContext], None] | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _set_step(session_factory: sessionmaker, task_id: int, idx: int, **fields: Any) -> None:
    """Commit a change to one step record. Reassigns the whole list so JSONB change is seen."""
    with session_factory() as s, s.begin():
        task = s.get(Task, task_id)
        steps = [dict(x) for x in task.steps]
        steps[idx].update(fields)
        task.steps = steps


def _finish(session_factory: sessionmaker, task_id: int, state: TaskState, error: str | None) -> TaskState:
    with session_factory() as s, s.begin():
        task = s.get(Task, task_id)
        task.state = state
        task.error = error
        task.finished_at = func.now()
    logger.info("task finished: %s", state.value)
    return state


def _load(session_factory: sessionmaker, task_id: int) -> Task:
    with session_factory() as s:
        task = s.get(Task, task_id)
        s.expunge(task)
        return task


def _ctx(task: Task, session_factory: sessionmaker, driver: HostDriver, settings: Settings) -> StepContext:
    return StepContext(
        task_id=task.id,
        target_id=task.target_id,
        params=dict(task.params),
        driver=driver,
        session_factory=session_factory,
        settings=settings,
        log=logger,
    )


def run_task(
    task_id: int,
    session_factory: sessionmaker,
    driver: HostDriver,
    settings: Settings,
    registry: Mapping[str, TaskType],
) -> TaskState:
    with task_context(task_id):
        task = _load(session_factory, task_id)
        tt = registry.get(task.type)
        if tt is None:
            return _finish(session_factory, task_id, TaskState.FAILED, f"unknown task type: {task.type}")
        if not task.steps:
            with session_factory() as s, s.begin():
                s.get(Task, task_id).steps = [
                    {"name": st.name, "status": "pending", "started_at": None, "finished_at": None, "error": None}
                    for st in tt.steps
                ]
            task = _load(session_factory, task_id)
        ctx = _ctx(task, session_factory, driver, settings)
        for idx, step in enumerate(tt.steps):
            if task.steps[idx]["status"] == "done":
                continue
            _set_step(session_factory, task_id, idx, status="running", started_at=_now(), finished_at=None, error=None)
            try:
                step.do(ctx)
            except Exception as e:  # noqa: BLE001
                logger.warning("step %s failed: %s", step.name, e)
                _set_step(session_factory, task_id, idx, status="failed", finished_at=_now(), error=str(e))
                return rollback_task(
                    task_id, session_factory, driver, settings, registry, reason=f"step {step.name} failed: {e}"
                )
            _set_step(session_factory, task_id, idx, status="done", finished_at=_now())
        return _finish(session_factory, task_id, TaskState.SUCCEEDED, None)


def rollback_task(
    task_id: int,
    session_factory: sessionmaker,
    driver: HostDriver,
    settings: Settings,
    registry: Mapping[str, TaskType],
    reason: str,
) -> TaskState:
    with task_context(task_id):
        task = _load(session_factory, task_id)
        tt = registry.get(task.type)
        if tt is None:
            return _finish(session_factory, task_id, TaskState.FAILED, f"unknown task type: {task.type}")
        ctx = _ctx(task, session_factory, driver, settings)
        undo_errors: list[str] = []
        for idx in reversed(range(len(task.steps))):
            if task.steps[idx]["status"] not in ("done", "failed", "running", "undo_failed"):
                continue
            step = tt.steps[idx]
            try:
                if step.undo is not None:
                    step.undo(ctx)
            except Exception as e:  # noqa: BLE001
                logger.error("undo of %s failed: %s", step.name, e)
                _set_step(session_factory, task_id, idx, status="undo_failed", finished_at=_now(), error=str(e))
                undo_errors.append(f"undo of {step.name} failed: {e}")
                continue
            _set_step(session_factory, task_id, idx, status="undone", finished_at=_now())
        if undo_errors:
            return _finish(session_factory, task_id, TaskState.FAILED, "; ".join([reason, *undo_errors]))
        if tt.on_rolled_back is not None:
            try:
                tt.on_rolled_back(ctx)
            except Exception as e:  # noqa: BLE001
                return _finish(session_factory, task_id, TaskState.FAILED, f"{reason}; on_rolled_back failed: {e}")
        return _finish(session_factory, task_id, TaskState.ROLLED_BACK, reason)
