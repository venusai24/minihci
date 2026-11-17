import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from minihci.api.deps import get_session
from minihci.api.idempotency import fingerprint, run_idempotent
from minihci.models import Host, Migration, Task, Vm
from minihci.paths import resolve_image
from minihci.scheduler.config import load_config
from minihci.scheduler.placement import PLACEABLE_STATUSES, NoCapacity, fits, place
from minihci.tasks.engine import enqueue
from minihci.tasks.locks import LOCK_NAMESPACE

log = logging.getLogger("minihci.api")
router = APIRouter()


class VmIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    host_id: int | None = None
    vcpus: int = Field(ge=1)
    mem_mb: int = Field(ge=64)
    image: str = Field(min_length=1)


def _auto_place(s: Session, body: VmIn) -> Host:
    """Pick a host for a VM with no host_id (D-022). Serialised so two creates cannot take the last slot."""
    s.execute(text("SELECT pg_advisory_xact_lock(:ns, 0)"), {"ns": LOCK_NAMESPACE["scheduler"]})
    hosts = list(
        s.scalars(select(Host).where(Host.status.in_(PLACEABLE_STATUSES)).order_by(Host.id).with_for_update(read=True))
    )
    rows = s.execute(
        select(Vm.host_id, func.sum(Vm.vcpus), func.sum(Vm.mem_mb))
        .where(Vm.desired_state != "absent")
        .group_by(Vm.host_id)
    )
    usage = {hid: (int(cpu), int(mem)) for hid, cpu, mem in rows}
    try:
        return place(body, hosts, usage, load_config(s))
    except NoCapacity:
        raise HTTPException(409, "no host has capacity for this vm") from None


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
        if body.host_id is not None:
            host = s.scalars(select(Host).where(Host.id == body.host_id).with_for_update(read=True)).first()
            if host is None:
                raise HTTPException(404, "host not found")
            if host.status != "ready":
                raise HTTPException(409, f"host is not ready (status={host.status})")
        host = _auto_place(s, body) if body.host_id is None else host
        if s.scalars(select(Vm).where(Vm.name == body.name, Vm.desired_state != "absent")).first() is not None:
            raise HTTPException(409, "vm name already exists")
        vm = Vm(
            name=body.name,
            host_id=host.id,
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
    log.info("task_enqueued", extra={"fields": {"task_id": task.id}})
    return JSONResponse({"task_id": task.id, "target_id": task.target_id}, status_code=202, headers=headers)


def _vm_out(v: Vm) -> dict[str, Any]:
    return {
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


@router.get("/vms")
def list_vms(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [_vm_out(v) for v in session.scalars(select(Vm).order_by(Vm.id))]


@router.get("/vms/{vm_id}")
def get_vm(vm_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
    vm = session.get(Vm, vm_id)
    if vm is None:
        raise HTTPException(404, "vm not found")
    return _vm_out(vm)


@router.get("/vms/{vm_id}/migrations")
def list_vm_migrations(vm_id: int, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    if session.get(Vm, vm_id) is None:
        raise HTTPException(404, "vm not found")
    rows = session.scalars(
        select(Migration).where(Migration.vm_id == vm_id).order_by(Migration.started_at.desc(), Migration.id.desc())
    )
    return [
        {
            "task_id": m.task_id,
            "src_host_id": m.src_host_id,
            "dst_host_id": m.dst_host_id,
            "status": m.status,
            "duration_ms": m.duration_ms,
            "downtime_ms": m.downtime_ms,
            "started_at": m.started_at.isoformat() if m.started_at else None,
            "finished_at": m.finished_at.isoformat() if m.finished_at else None,
        }
        for m in rows
    ]


class StopIn(BaseModel):
    force: bool = False


_DESIRED = {"start": "running", "reboot": "running", "stop": "stopped", "delete": None}


def _vm_op(request: Request, vm_id: int, op: str, idempotency_key: str | None, params: dict[str, Any]) -> JSONResponse:
    def create(s: Session):
        vm = s.scalars(select(Vm).where(Vm.id == vm_id).with_for_update()).first()
        if vm is None or vm.desired_state == "absent":
            raise HTTPException(404, "vm not found")
        deleting = s.scalars(
            select(Task.id).where(
                Task.type == "vm.delete", Task.target_id == vm_id, Task.state.in_(("pending", "running"))
            )
        ).first()
        if deleting is not None:
            raise HTTPException(409, "vm is being deleted")
        if _DESIRED[op] is not None:
            vm.desired_state = _DESIRED[op]
        return enqueue(s, f"vm.{op}", vm_id, params)

    task, created = run_idempotent(
        request.app.state.session_factory,
        idempotency_key,
        fingerprint({"op": f"vm.{op}", "vm_id": vm_id, **params}),
        create,
    )
    headers = {"Location": f"/tasks/{task.id}"}
    if not created:
        headers["Idempotent-Replayed"] = "true"
    log.info("task_enqueued", extra={"fields": {"task_id": task.id}})
    return JSONResponse({"task_id": task.id, "target_id": task.target_id}, status_code=202, headers=headers)


@router.post("/vms/{vm_id}/start", status_code=202)
def start_vm(vm_id: int, request: Request, idempotency_key: Annotated[str | None, Header()] = None):
    return _vm_op(request, vm_id, "start", idempotency_key, {})


@router.post("/vms/{vm_id}/reboot", status_code=202)
def reboot_vm(vm_id: int, request: Request, idempotency_key: Annotated[str | None, Header()] = None):
    return _vm_op(request, vm_id, "reboot", idempotency_key, {})


@router.post("/vms/{vm_id}/stop", status_code=202)
def stop_vm(
    vm_id: int,
    request: Request,
    body: StopIn | None = None,
    idempotency_key: Annotated[str | None, Header()] = None,
):
    return _vm_op(request, vm_id, "stop", idempotency_key, {"force": (body or StopIn()).force})


@router.delete("/vms/{vm_id}", status_code=202)
def delete_vm(vm_id: int, request: Request, idempotency_key: Annotated[str | None, Header()] = None):
    return _vm_op(request, vm_id, "delete", idempotency_key, {})


class MigrateIn(BaseModel):
    host_id: int


@router.post("/vms/{vm_id}/migrate", status_code=202)
def migrate_vm(
    vm_id: int,
    body: MigrateIn,
    request: Request,
    idempotency_key: Annotated[str | None, Header()] = None,
):
    def create(s: Session):
        vm = s.scalars(select(Vm).where(Vm.id == vm_id).with_for_update()).first()
        if vm is None or vm.desired_state == "absent":
            raise HTTPException(404, "vm not found")
        if vm.actual_state != "running" or vm.desired_state != "running":
            raise HTTPException(409, "vm is not running")
        busy = s.scalars(
            select(Task.id).where(
                Task.type.in_(("vm.delete", "vm.migrate")),
                Task.target_id == vm_id,
                Task.state.in_(("pending", "running")),
            )
        ).first()
        if busy is not None:
            raise HTTPException(409, "vm has an open delete or migrate task")
        if body.host_id == vm.host_id:
            raise HTTPException(409, "vm is already on that host")
        s.execute(text("SELECT pg_advisory_xact_lock(:ns, 0)"), {"ns": LOCK_NAMESPACE["scheduler"]})
        dst = s.scalars(select(Host).where(Host.id == body.host_id).with_for_update(read=True)).first()
        if dst is None or dst.status not in PLACEABLE_STATUSES:
            raise HTTPException(409, "destination host is not placeable")
        rows = s.execute(
            select(Vm.host_id, func.sum(Vm.vcpus), func.sum(Vm.mem_mb))
            .where(Vm.desired_state != "absent", Vm.id != vm_id)
            .group_by(Vm.host_id)
        )
        usage = {hid: (int(cpu), int(mem)) for hid, cpu, mem in rows}
        if not fits(vm, dst, usage, load_config(s)):
            raise HTTPException(409, "destination host has no capacity for this vm")
        return enqueue(s, "vm.migrate", vm_id, {"src_host_id": vm.host_id, "dst_host_id": dst.id})

    task, created = run_idempotent(
        request.app.state.session_factory,
        idempotency_key,
        fingerprint({"op": "vm.migrate", "vm_id": vm_id, "host_id": body.host_id}),
        create,
    )
    headers = {"Location": f"/tasks/{task.id}"}
    if not created:
        headers["Idempotent-Replayed"] = "true"
    log.info("task_enqueued", extra={"fields": {"task_id": task.id}})
    return JSONResponse({"task_id": task.id, "target_id": task.target_id}, status_code=202, headers=headers)
