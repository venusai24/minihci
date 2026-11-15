import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from minihci.config import Settings
from minihci.driver.base import HostDriver
from minihci.logging import task_context
from minihci.models import Task
from minihci.tasks.engine import TaskState
from minihci.tasks.executor import TaskType, _finish, rollback_task
from minihci.tasks.registry import REGISTRY

logger = logging.getLogger("minihci.recovery")


@dataclass
class RecoveryReport:
    resumed: list[int] = field(default_factory=list)
    rolled_back: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)


def recover_orphans(
    session_factory: sessionmaker,
    driver: HostDriver,
    settings: Settings,
    registry: Mapping[str, TaskType] = REGISTRY,
) -> RecoveryReport:
    """Resolve tasks left `running` by a dead worker. Call once at process start, before claiming."""
    report = RecoveryReport()
    with session_factory() as s:
        orphans = [(t.id, t.type, t.attempts) for t in s.scalars(
            select(Task).where(Task.state == TaskState.RUNNING).order_by(Task.id)
        )]
    for task_id, type_, attempts in orphans:
        with task_context(task_id):
            if type_ not in registry:
                logger.warning("orphan has unknown type %s; failing", type_)
                _finish(session_factory, task_id, TaskState.FAILED, f"unknown task type: {type_}")
                report.failed.append(task_id)
            elif attempts >= settings.max_attempts:
                logger.warning("orphan at max attempts (%d); rolling back", attempts)
                state = rollback_task(
                    task_id, session_factory, driver, settings, registry,
                    reason=f"max attempts exceeded ({attempts})",
                )
                (report.rolled_back if state == TaskState.ROLLED_BACK else report.failed).append(task_id)
            else:
                logger.info("orphan requeued for resume (attempt %d)", attempts)
                with session_factory() as s, s.begin():
                    s.get(Task, task_id).state = TaskState.PENDING
                report.resumed.append(task_id)
    return report
