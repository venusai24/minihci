import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from minihci.api.deps import get_session
from minihci.api.idempotency import fingerprint, run_idempotent
from minihci.dr.chain import RPO_WINDOW, ReplicaInfo, rpo_status
from minihci.models import DrFailover, Host, ProtectionPolicy, Replica, Task, Vm
from minihci.scheduler.config import load_config
from minihci.scheduler.placement import PLACEABLE_STATUSES, fits
from minihci.tasks.engine import enqueue
from minihci.tasks.locks import LOCK_NAMESPACE

log = logging.getLogger("minihci.api")
router = APIRouter()


class ProtectionIn(BaseModel):
    interval_s: int = Field(ge=30, le=86400)
    retention: int = Field(ge=1, le=100)
    enabled: bool = True


class FailoverIn(BaseModel):
    replica_id: int | None = None
    host_id: int | None = None
    force: bool = False


def _now() -> datetime:
    return datetime.now(UTC)


def _ts(t: datetime | None) -> str | None:
    """Same format api/hosts.py uses for last_heartbeat (UTC, Z suffix, no zero microseconds)."""
    if t is None:
        return None
    if t.tzinfo is not None:
        t = t.astimezone(UTC)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ").replace(".000000Z", "Z")


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)


@router.put("/vms/{vm_id}/protection", status_code=202)
def put_protection(
    vm_id: int,
    body: ProtectionIn,
    request: Request,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    def create(s: Session):
        vm = s.scalars(select(Vm).where(Vm.id == vm_id).with_for_update()).first()
        if vm is None or vm.desired_state == "absent":
            raise HTTPException(404, "vm not found")
        host = s.get(Host, vm.host_id)
        if host is not None and host.site == "dr":
            raise HTTPException(409, "vm runs at the DR site; protection cannot be changed")
        busy = s.scalars(
            select(Task.id).where(
                Task.type == "vm.delete", Task.target_id == vm_id, Task.state.in_(("pending", "running"))
            )
        ).first()
        if busy is not None:
            raise HTTPException(409, "vm has an open delete task")
        return enqueue(s, "vm.protection_set", vm_id, body.model_dump())

    task, created = run_idempotent(
        request.app.state.session_factory,
        idempotency_key,
        fingerprint({"op": "vm.protection_set", "vm_id": vm_id, **body.model_dump()}),
        create,
    )
    headers = {"Location": f"/tasks/{task.id}"}
    if not created:
        headers["Idempotent-Replayed"] = "true"
    log.info("task_enqueued", extra={"fields": {"task_id": task.id}})
    return JSONResponse({"task_id": task.id, "target_id": task.target_id}, status_code=202, headers=headers)


def protection_status(s: Session, p: ProtectionPolicy, now: datetime) -> dict[str, Any]:
    """lag_s / last_replicated_at come from the newest succeeded replica regardless of age (a stale VM must
    still report its lag); achieved_rpo_s comes from succeeded+pruned rows inside RPO_WINDOW, filtered in SQL."""
    cutoff = now - RPO_WINDOW
    rows = s.scalars(
        select(Replica)
        .where(
            Replica.vm_id == p.vm_id,
            Replica.status.in_(("succeeded", "pruned")),
            Replica.snapshot_time >= cutoff,
        )
        .order_by(Replica.snapshot_time, Replica.id)
    )
    history = [
        ReplicaInfo(
            id=r.id,
            chain_id=r.chain_id or 0,
            seq=r.seq,
            dr_path=r.dr_path or "",
            checkpoint=r.checkpoint or "",
            snapshot_time=_aware(r.snapshot_time),
            finished_at=_aware(r.finished_at or r.snapshot_time),
        )
        for r in rows
    ]
    ok = (Replica.vm_id == p.vm_id, Replica.status == "succeeded")
    replica_count = s.scalar(select(func.count()).select_from(Replica).where(*ok)) or 0
    newest = s.scalar(select(func.max(Replica.snapshot_time)).where(*ok))
    st = rpo_status(history, p.interval_s, now)  # adapter: only achieved_rpo_s and target_s are used from it
    lag = (now - _aware(newest)).total_seconds() if newest is not None else None
    within = lag is not None and max(st.achieved_rpo_s or 0, lag) <= st.target_s
    return {
        "vm_id": p.vm_id,
        "interval_s": p.interval_s,
        "retention": p.retention,
        "enabled": p.enabled,
        "last_replicated_at": _ts(newest),
        "lag_s": lag,
        "achieved_rpo_s": st.achieved_rpo_s,
        "rpo_target_s": st.target_s,
        "within_target": within,
        "replica_count": replica_count,
    }


@router.get("/vms/{vm_id}/protection")
def get_protection(vm_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    p = session.get(ProtectionPolicy, vm_id)
    if p is None:
        raise HTTPException(404, "no protection policy for this vm")
    return protection_status(session, p, _now())


@router.get("/vms/{vm_id}/replicas")
def list_replicas(vm_id: int, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    if session.get(Vm, vm_id) is None:
        raise HTTPException(404, "vm not found")
    rows = session.scalars(select(Replica).where(Replica.vm_id == vm_id).order_by(Replica.id.desc()))
    return [
        {
            "id": r.id,
            "mode": r.mode,
            "status": r.status,
            "chain_id": r.chain_id,
            "seq": r.seq,
            "snapshot_time": _ts(r.snapshot_time),
            "finished_at": _ts(r.finished_at),
            "duration_ms": r.duration_ms,
            "size_bytes": r.size_bytes,
            "dr_path": r.dr_path,
        }
        for r in rows
    ]


@router.get("/vms/{vm_id}/failovers")
def list_failovers(vm_id: int, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """FR-5.4: the stored failover numbers, newest first. data_loss_s is a lower bound (D-037): it is measured from
    the old host's last heartbeat, which precedes the real failure, and is null for a forced failover of a live host;
    failover_ms runs from the task's record step to the switch."""
    if session.get(Vm, vm_id) is None:
        raise HTTPException(404, "vm not found")
    rows = session.scalars(select(DrFailover).where(DrFailover.vm_id == vm_id).order_by(DrFailover.id.desc()))
    return [
        {
            "id": r.id,
            "vm_id": r.vm_id,
            "task_id": r.task_id,
            "replica_id": r.replica_id,
            "from_host_id": r.from_host_id,
            "dr_host_id": r.dr_host_id,
            "forced": r.forced,
            "primary_failed_at": _ts(r.primary_failed_at),
            "started_at": _ts(r.started_at),
            "running_at": _ts(r.running_at),
            "failover_ms": r.failover_ms,
            "data_loss_s": r.data_loss_s,
            "from_disk_path": r.from_disk_path,
            "disk_path": r.disk_path,
        }
        for r in rows
    ]


@router.get("/replication")
def replication(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    now = _now()
    rows = session.execute(
        select(ProtectionPolicy, Vm.name)
        .join(Vm, Vm.id == ProtectionPolicy.vm_id)
        .where(Vm.desired_state != "absent")
        .order_by(ProtectionPolicy.vm_id)
    ).all()
    return [{**protection_status(session, p, now), "vm_name": name} for p, name in rows]


@router.post("/vms/{vm_id}/failover", status_code=202)
def failover_vm(
    vm_id: int,
    request: Request,
    body: FailoverIn | None = None,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """D-037. Checks run in one transaction holding the VM row FOR UPDATE and the scheduler lock (3, 0); the task
    re-checks the fence itself under (3, 0). The resolved replica and DR host go into the params, so a replay with
    the same Idempotency-Key returns the original task even if a newer replica has appeared since."""
    body = body or FailoverIn()

    def create(s: Session):
        vm = s.scalars(select(Vm).where(Vm.id == vm_id).with_for_update()).first()
        if vm is None or vm.desired_state == "absent":
            raise HTTPException(404, "vm not found")
        # Before the scheduler lock, as the HA failover pass does (no deadlock with vm.migrate.check_destination).
        busy = s.scalars(
            select(Task.id).where(
                Task.type.like("vm.%"), Task.target_id == vm_id, Task.state.in_(("pending", "running"))
            )
        ).first()
        if busy is not None:
            raise HTTPException(409, "vm has an open task")
        s.execute(text("SELECT pg_advisory_xact_lock(:ns, 0)"), {"ns": LOCK_NAMESPACE["scheduler"]})
        src = s.get(Host, vm.host_id)
        if src.site == "dr":
            raise HTTPException(409, "vm already runs at the DR site")
        if vm.desired_state == "stopped":
            raise HTTPException(
                409, "vm is stopped; failover would start a VM the user stopped; start it first or use host.reclaim"
            )
        if src.status != "dead" and not body.force:
            raise HTTPException(409, f"host {src.name} is {src.status}, not dead; use force to fail over anyway")
        if body.replica_id is not None:
            replica = s.get(Replica, body.replica_id)
            if replica is None or replica.vm_id != vm_id or replica.status != "succeeded":
                raise HTTPException(409, f"replica {body.replica_id} is not a succeeded replica of this vm")
        else:
            replica = s.scalars(
                select(Replica)
                .where(Replica.vm_id == vm_id, Replica.status == "succeeded")
                .order_by(Replica.snapshot_time.desc(), Replica.id.desc())
                .limit(1)
            ).first()
            if replica is None:
                raise HTTPException(409, "vm has no succeeded replica")
        q = select(Host).where(Host.site == "dr", Host.status.in_(PLACEABLE_STATUSES))
        if body.host_id is not None:
            q = q.where(Host.id == body.host_id)
        candidates = list(s.scalars(q.order_by(Host.id).with_for_update(read=True)))
        if not candidates:
            raise HTTPException(
                409, "no ready DR-site host" if body.host_id is None else "host is not a ready DR-site host"
            )
        rows = s.execute(
            select(Vm.host_id, func.sum(Vm.vcpus), func.sum(Vm.mem_mb))
            .where(Vm.desired_state != "absent", Vm.id != vm_id)
            .group_by(Vm.host_id)
        )
        usage = {hid: (int(cpu), int(mem)) for hid, cpu, mem in rows}
        config = load_config(s)
        # the only caller that passes allow_dr (D-033)
        dr_host = next((h for h in candidates if fits(vm, h, usage, config, allow_dr=True)), None)
        if dr_host is None:
            raise HTTPException(409, "DR-site host has no capacity for this vm")
        params = {"replica_id": replica.id, "dr_host_id": dr_host.id, "force": body.force}
        return enqueue(s, "vm.dr_failover", vm_id, params)

    task, created = run_idempotent(
        request.app.state.session_factory,
        idempotency_key,
        fingerprint(
            {
                "op": "vm.dr_failover",
                "vm_id": vm_id,
                "replica_id": body.replica_id,
                "host_id": body.host_id,
                "force": body.force,
            }
        ),
        create,
    )
    headers = {"Location": f"/tasks/{task.id}"}
    if not created:
        headers["Idempotent-Replayed"] = "true"
    log.info("task_enqueued", extra={"fields": {"task_id": task.id}})
    return JSONResponse({"task_id": task.id, "target_id": task.target_id}, status_code=202, headers=headers)
