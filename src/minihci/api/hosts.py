import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from minihci.api.deps import get_session
from minihci.api.idempotency import fingerprint, run_idempotent
from minihci.models import Host, Vm
from minihci.tasks.engine import enqueue

log = logging.getLogger("minihci.api")
router = APIRouter()


class HostIn(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    libvirt_uri: str = Field(min_length=1)
    site: Literal["primary", "dr"] = "primary"


@router.post("/hosts", status_code=202)
def register_host(
    body: HostIn,
    request: Request,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    def create(s: Session):
        taken = s.scalars(
            select(Host).where(
                Host.status != "removed", or_(Host.name == body.name, Host.libvirt_uri == body.libvirt_uri)
            )
        ).first()
        if taken is not None:
            raise HTTPException(409, "host name or libvirt_uri already registered")
        host = Host(name=body.name, libvirt_uri=body.libvirt_uri, site=body.site, status="registering")
        s.add(host)
        s.flush()
        return enqueue(s, "host.register", host.id, {})

    task, created = run_idempotent(
        request.app.state.session_factory, idempotency_key, fingerprint(body.model_dump()), create
    )
    headers = {"Location": f"/tasks/{task.id}"}
    if not created:
        headers["Idempotent-Replayed"] = "true"
    log.info("task_enqueued", extra={"fields": {"task_id": task.id}})
    return JSONResponse({"task_id": task.id, "target_id": task.target_id}, status_code=202, headers=headers)


@router.delete("/hosts/{host_id}", status_code=202)
def remove_host(
    host_id: int,
    request: Request,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    def create(s: Session):
        # FOR UPDATE serializes with VM creation, which takes a shared row lock on the host.
        host = s.scalars(select(Host).where(Host.id == host_id).with_for_update()).first()
        if host is None or host.status == "removed":
            raise HTTPException(404, "host not found")
        if host.status in ("registering", "removing"):
            raise HTTPException(409, f"host is busy (status={host.status})")
        names = list(
            s.scalars(select(Vm.name).where(Vm.host_id == host_id, Vm.desired_state != "absent").order_by(Vm.name))
        )
        if names:
            raise HTTPException(409, f"host has {len(names)} VM(s): {', '.join(names)}")
        prev = host.status
        host.status = "removing"
        return enqueue(s, "host.remove", host.id, {"prev_status": prev})

    task, created = run_idempotent(
        request.app.state.session_factory,
        idempotency_key,
        fingerprint({"op": "host.remove", "host_id": host_id}),
        create,
    )
    headers = {"Location": f"/tasks/{task.id}"}
    if not created:
        headers["Idempotent-Replayed"] = "true"
    log.info("task_enqueued", extra={"fields": {"task_id": task.id}})
    return JSONResponse({"task_id": task.id, "target_id": task.target_id}, status_code=202, headers=headers)


@router.get("/hosts")
def list_hosts(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [
        {
            "id": h.id,
            "name": h.name,
            "libvirt_uri": h.libvirt_uri,
            "status": h.status,
            "site": h.site,
            "cpu_total": h.cpu_total,
            "mem_total_mb": h.mem_total_mb,
        }
        for h in session.scalars(select(Host).where(Host.status != "removed").order_by(Host.id))
    ]


@router.get("/hosts/{host_id}/health")
def host_health(host_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    h = session.get(Host, host_id)
    if h is None or h.status == "removed":
        raise HTTPException(404, "host not found")
    beat = h.last_heartbeat  # naive-UTC column
    return {
        "host_id": h.id,
        "status": h.status,
        "last_heartbeat": beat.strftime("%Y-%m-%dT%H:%M:%S.%fZ").replace(".000000Z", "Z") if beat else None,
        "missed_heartbeats": h.missed_heartbeats,
    }
