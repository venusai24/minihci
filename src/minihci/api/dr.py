import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from minihci.api.deps import get_session
from minihci.api.idempotency import fingerprint, run_idempotent
from minihci.dr.chain import RPO_WINDOW, ReplicaInfo, rpo_status
from minihci.models import Host, ProtectionPolicy, Replica, Task, Vm
from minihci.tasks.engine import enqueue

log = logging.getLogger("minihci.api")
router = APIRouter()


class ProtectionIn(BaseModel):
    interval_s: int = Field(ge=30, le=86400)
    retention: int = Field(ge=1, le=100)
    enabled: bool = True


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
