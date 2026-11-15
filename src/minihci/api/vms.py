from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from minihci.api.deps import get_session
from minihci.api.idempotency import fingerprint, run_idempotent
from minihci.models import Host, Vm
from minihci.paths import resolve_image
from minihci.tasks.engine import enqueue

router = APIRouter()


class VmIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    host_id: int
    vcpus: int = Field(ge=1)
    mem_mb: int = Field(ge=64)
    image: str = Field(min_length=1)


@router.post("/vms", status_code=202)
def create_vm(
    body: VmIn,
    request: Request,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    settings = request.app.state.settings
    try:
        resolve_image(body.image, settings.image_root)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None

    def create(s: Session):
        # Runs only on an idempotency-key miss, so a replay never trips the name check below.
        host = s.get(Host, body.host_id)
        if host is None:
            raise HTTPException(404, "host not found")
        if host.status != "ready":
            raise HTTPException(409, f"host is not ready (status={host.status})")
        if s.scalars(select(Vm).where(Vm.name == body.name, Vm.desired_state != "absent")).first() is not None:
            raise HTTPException(409, "vm name already exists")
        vm = Vm(
            name=body.name,
            host_id=body.host_id,
            vcpus=body.vcpus,
            mem_mb=body.mem_mb,
            image=body.image,
            disk_path=f"{settings.storage_root}/{body.name}.qcow2",
            desired_state="running",
            actual_state="pending",
        )
        s.add(vm)
        s.flush()
        return enqueue(s, "vm.create", vm.id, {})

    try:
        task, created = run_idempotent(
            request.app.state.session_factory, idempotency_key, fingerprint(body.model_dump()), create
        )
    except IntegrityError:  # lost a race on the unique vm name
        raise HTTPException(409, "vm name already exists") from None
    headers = {"Location": f"/tasks/{task.id}"}
    if not created:
        headers["Idempotent-Replayed"] = "true"
    return JSONResponse({"task_id": task.id, "target_id": task.target_id}, status_code=202, headers=headers)


@router.get("/vms")
def list_vms(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [
        {
            "id": v.id,
            "name": v.name,
            "host_id": v.host_id,
            "vcpus": v.vcpus,
            "mem_mb": v.mem_mb,
            "image": v.image,
            "disk_path": v.disk_path,
            "desired_state": v.desired_state,
            "actual_state": v.actual_state,
        }
        for v in session.scalars(select(Vm).order_by(Vm.id))
    ]
