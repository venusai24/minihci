import logging
import threading
import uuid
from collections.abc import Mapping
from contextlib import nullcontext

from sqlalchemy.orm import sessionmaker

from minihci.config import Settings
from minihci.driver.base import HostDriver
from minihci.tasks.engine import claim_next
from minihci.tasks.executor import TaskType, run_task
from minihci.tasks.locks import target_lock
from minihci.tasks.registry import REGISTRY

logger = logging.getLogger("minihci.worker")


class Worker:
    def __init__(
        self,
        session_factory: sessionmaker,
        driver: HostDriver,
        settings: Settings,
        registry: Mapping[str, TaskType] = REGISTRY,
        worker_id: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.driver = driver
        self.settings = settings
        self.registry = registry
        self.worker_id = worker_id or uuid.uuid4().hex[:8]
        self.engine = session_factory.kw["bind"]

    def run_once(self) -> int | None:
        """Claim one task, take its per-target lock, run it. Returns the task id, or None if idle."""
        task = claim_next(self.session_factory)
        if task is None:
            return None
        tt = self.registry.get(task.type)
        lock = target_lock(self.engine, tt.target_kind, task.target_id) if tt else nullcontext()
        with lock:
            run_task(task.id, self.session_factory, self.driver, self.settings, self.registry)
        return task.id

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                if self.run_once() is None:
                    stop.wait(self.settings.worker_poll_interval_s)
            except Exception:  # noqa: BLE001
                logger.exception("worker loop error")
                stop.wait(self.settings.worker_poll_interval_s)
