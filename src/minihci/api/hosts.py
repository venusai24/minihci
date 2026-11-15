from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from minihci.api.deps import get_session
from minihci.api.idempotency import fingerprint, run_idempotent
from minihci.models import Host
from minihci.tasks.engine import enqueue

router = APIRouter()


class HostIn(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    libvirt_uri: str = Field(min_length=1)


@router.post("/hosts", status_code=202)
def register_host(
    body: HostIn,
    request: Request,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    def create(s: Session):
        taken = s.scalars(select(Host).where(or_(Host.name == body.name, Host.libvirt_uri == body.libvirt_uri))).first()
        if taken is not None:
            raise HTTPException(409, "host name or libvirt_uri already registered")
        host = Host(name=body.name, libvirt_uri=body.libvirt_uri, status="registering")
        s.add(host)
        s.flush()
        return enqueue(s, "host.register", host.id, {})

    task, created = run_idempotent(
        request.app.state.session_factory, idempotency_key, fingerprint(body.model_dump()), create
    )
    headers = {"Location": f"/tasks/{task.id}"}
    if not created:
        headers["Idempotent-Replayed"] = "true"
    return JSONResponse({"task_id": task.id, "target_id": task.target_id}, status_code=202, headers=headers)


@router.get("/hosts")
def list_hosts(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [
        {
            "id": h.id,
            "name": h.name,
            "libvirt_uri": h.libvirt_uri,
            "status": h.status,
            "cpu_total": h.cpu_total,
            "mem_total_mb": h.mem_total_mb,
        }
        for h in session.scalars(select(Host).order_by(Host.id))
    ]
