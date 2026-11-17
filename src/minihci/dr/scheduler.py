"""Level-triggered replication scheduler (D-036). Reads and writes the DB only, never calls a driver."""

import logging
import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from minihci.config import Settings
from minihci.models import Host, ProtectionPolicy, Replica, Task, Vm
from minihci.tasks.engine import enqueue

log = logging.getLogger("minihci.dr.scheduler")


def _candidates(session_factory: sessionmaker[Session]) -> list[int]:
    with session_factory() as s:
        return list(
            s.scalars(
                select(Vm.id)
                .join(ProtectionPolicy, ProtectionPolicy.vm_id == Vm.id)
                .join(Host, Host.id == Vm.host_id)
                .where(
                    ProtectionPolicy.enabled,
                    Vm.desired_state == "running",
                    Vm.actual_state == "running",
                    Host.status == "ready",
                    Host.site == "primary",
                )
                .order_by(Vm.id)
            )
        )


def _handle(session_factory: sessionmaker[Session], vm_id: int, settings: Settings, now: datetime) -> int | None:
    with session_factory() as s, s.begin():
        # FOR NO KEY UPDATE SKIP LOCKED: a VM busy elsewhere is retried on the next pass.
        vm = s.scalars(select(Vm).where(Vm.id == vm_id).with_for_update(skip_locked=True, key_share=True)).first()
        if vm is None or vm.desired_state != "running" or vm.actual_state != "running":
            return None
        policy = s.get(ProtectionPolicy, vm_id)
        host = s.get(Host, vm.host_id)
        if policy is None or host is None or not policy.enabled or host.status != "ready" or host.site != "primary":
            return None
        open_task = s.scalars(
            select(Task.id).where(
                Task.type.like("vm.%"), Task.target_id == vm_id, Task.state.in_(("pending", "running"))
            )
        ).first()
        if open_task is not None:
            return None
        # tasks.finished_at is a naive UTC timestamp; replicas.snapshot_time is timestamptz.
        failed_at = s.scalar(
            select(func.max(Task.finished_at)).where(
                Task.type == "vm.replicate", Task.target_id == vm_id, Task.state.in_(("failed", "rolled_back"))
            )
        )
        retry = timedelta(seconds=settings.replication_retry_s)
        if failed_at is not None and now - failed_at.replace(tzinfo=UTC) < retry:
            return None
        last = s.scalar(
            select(func.max(Replica.snapshot_time)).where(Replica.vm_id == vm_id, Replica.status == "succeeded")
        )
        if last is not None and now - last < timedelta(seconds=policy.interval_s):
            return None
        task_id = enqueue(s, "vm.replicate", vm_id, {}).id
    log.info(
        "replicate_enqueued", extra={"fields": {"event": "replicate_enqueued", "task_id": task_id, "vm_id": vm_id}}
    )
    return task_id


def replicate_once(
    session_factory: sessionmaker[Session], settings: Settings, now: datetime | None = None
) -> list[int]:
    now = now or datetime.now(UTC)
    ids = []
    for vm_id in _candidates(session_factory):
        try:
            # the per-VM transaction is closed by _handle's context managers, so a failure releases the row lock
            task_id = _handle(session_factory, vm_id, settings, now)
        except Exception:
            log.exception("replication check failed", extra={"fields": {"event": "replicate_failed", "vm_id": vm_id}})
            continue
        if task_id is not None:
            ids.append(task_id)
    return ids


def run_forever(stop: threading.Event, session_factory: sessionmaker[Session], settings: Settings) -> None:
    while not stop.is_set():
        try:
            replicate_once(session_factory, settings)
        except Exception:
            log.exception("replication pass failed")
        stop.wait(settings.replication_tick_s)
