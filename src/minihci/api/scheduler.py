"""Scheduler configuration API (FR-3.2, D-022)."""

import logging
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from minihci.api.deps import get_session
from minihci.api.idempotency import fingerprint, run_idempotent
from minihci.scheduler.config import load_config
from minihci.tasks.engine import enqueue

log = logging.getLogger("minihci.api")
router = APIRouter()


class SchedulerConfigIn(BaseModel):
    cpu_headroom_pct: int | None = Field(default=None, ge=1, le=100)
    rebalance_enabled: bool | None = None
    rebalance_cpu_threshold_pct: int | None = Field(default=None, ge=1, le=100)
    rebalance_window_s: int | None = Field(default=None, ge=30)
    rebalance_vm_cooldown_s: int | None = Field(default=None, ge=0)
    rebalance_host_cooldown_s: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _at_least_one(self) -> "SchedulerConfigIn":
        if not self.model_dump(exclude_none=True):
            raise ValueError("at least one config field is required")
        return self


@router.get("/scheduler/config")
def get_scheduler_config(session: Session = Depends(get_session)) -> dict[str, int | bool]:
    return asdict(load_config(session))


@router.put("/scheduler/config", status_code=202)
def put_scheduler_config(
    body: SchedulerConfigIn,
    request: Request,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    def create(s: Session):
        return enqueue(s, "scheduler.config_set", 1, body.model_dump(exclude_none=True))

    task, created = run_idempotent(
        request.app.state.session_factory,
        idempotency_key,
        fingerprint({"op": "scheduler.config_set", **body.model_dump(exclude_none=True)}),
        create,
    )
    headers = {"Location": f"/tasks/{task.id}"}
    if not created:
        headers["Idempotent-Replayed"] = "true"
    log.info("task_enqueued", extra={"fields": {"task_id": task.id}})
    return JSONResponse({"task_id": task.id, "target_id": task.target_id}, status_code=202, headers=headers)
