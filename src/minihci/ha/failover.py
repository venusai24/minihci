"""Level-triggered failover pass (FR-4.2, FR-4.4, D-028). Reads and writes the DB only, never calls a driver."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from minihci.config import Settings
from minihci.models import Host, Task, Vm
from minihci.scheduler.config import load_config
from minihci.scheduler.placement import NoCapacity, place
from minihci.tasks.engine import enqueue
from minihci.tasks.locks import LOCK_NAMESPACE

log = logging.getLogger("minihci.failover")

# paused and crashed guests died with their host just the same (D-026 amendment 4)
RESTARTABLE = ("running", "pending_restart", "paused", "crashed")


def _candidates(session_factory: sessionmaker[Session]) -> list[int]:
    with session_factory() as s:
        return list(
            s.scalars(
                select(Vm.id)
                .join(Host, Host.id == Vm.host_id)
                .where(Host.status == "dead", Vm.desired_state == "running", Vm.actual_state.in_(RESTARTABLE))
                .order_by(Vm.id)
            )
        )


def _handle(session_factory: sessionmaker[Session], vm_id: int, settings: Settings) -> None:
    event: dict | None = None
    level = logging.INFO
    with session_factory() as s, s.begin():
        # Lock order matches the rebalancer: VM row first, then the scheduler advisory lock.
        # FOR NO KEY UPDATE SKIP LOCKED: a VM busy elsewhere is retried on the next pass.
        vm = s.scalars(select(Vm).where(Vm.id == vm_id).with_for_update(skip_locked=True, key_share=True)).first()
        if vm is None or vm.desired_state != "running" or vm.actual_state not in RESTARTABLE:
            return
        host = s.get(Host, vm.host_id)
        if host.status != "dead":
            return
        open_task = s.scalars(
            select(Task.id).where(
                Task.type.like("vm.%"),
                Task.target_id == vm.id,
                Task.state.in_(("pending", "running")),
            )
        ).first()
        if open_task is not None:
            return
        s.execute(text("SELECT pg_advisory_xact_lock(:ns, 0)"), {"ns": LOCK_NAMESPACE["scheduler"]})
        s.refresh(host)
        if host.status != "dead":
            return
        failed_at = (
            host.last_heartbeat.replace(tzinfo=UTC)
            if host.last_heartbeat
            else datetime.now(UTC) - timedelta(seconds=settings.heartbeat_dead_after_s)
        ).isoformat()
        # Same outage = same failed_at. Stop retrying a restart that keeps failing; the VM waits in pending_restart.
        failures = s.scalar(
            select(func.count(Task.id)).where(
                Task.type == "vm.ha_restart",
                Task.target_id == vm.id,
                Task.state.in_(("failed", "rolled_back")),
                Task.params["failed_at"].astext == failed_at,
            )
        )
        if failures >= settings.ha_restart_max_attempts:
            if vm.actual_state != "pending_restart":
                vm.actual_state = "pending_restart"
                event = {"event": "ha_restart_gave_up", "vm_id": vm.id, "dead_host_id": host.id, "failures": failures}
                level = logging.WARNING
        else:
            rows = s.execute(
                select(Vm.host_id, func.sum(Vm.vcpus), func.sum(Vm.mem_mb))
                .where(Vm.desired_state != "absent", Vm.id != vm.id)
                .group_by(Vm.host_id)
            )
            usage = {hid: (int(c), int(m)) for hid, c, m in rows}
            try:
                place(vm, list(s.scalars(select(Host).order_by(Host.id))), usage, load_config(s))
            except NoCapacity:
                if vm.actual_state != "pending_restart":
                    vm.actual_state = "pending_restart"
                    event = {"event": "ha_pending_restart", "vm_id": vm.id, "dead_host_id": host.id}
                    level = logging.WARNING
            else:
                task = enqueue(s, "vm.ha_restart", vm.id, {"dead_host_id": host.id, "failed_at": failed_at})
                event = {"event": "ha_restart_enqueued", "task_id": task.id, "vm_id": vm.id, "dead_host_id": host.id}
    if event:
        log.log(level, event["event"], extra={"fields": event})


def failover_once(session_factory: sessionmaker[Session], settings: Settings) -> None:
    for vm_id in _candidates(session_factory):
        _handle(session_factory, vm_id, settings)
