"""Scheduler configuration API (FR-3.2, D-022)."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from minihci.api.deps import get_session
from minihci.api.idempotency import fingerprint, run_idempotent
from minihci.scheduler.config import load_config
from minihci.tasks.engine import enqueue

log = logging.getLogger("minihci.api")
router = APIRouter()


class SchedulerConfigIn(BaseModel):
    cpu_headroom_pct: int = Field(ge=1, le=100)


@router.get("/scheduler/config")
def get_scheduler_config(session: Session = Depends(get_session)) -> dict[str, int]:
    return {"cpu_headroom_pct": load_config(session).cpu_headroom_pct}


@router.put("/scheduler/config", status_code=202)
def put_scheduler_config(
    body: SchedulerConfigIn,
    request: Request,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    def create(s: Session):
        return enqueue(s, "scheduler.config_set", 1, {"cpu_headroom_pct": body.cpu_headroom_pct})

    task, created = run_idempotent(
        request.app.state.session_factory,
        idempotency_key,
        fingerprint({"op": "scheduler.config_set", **body.model_dump()}),
        create,
    )
    headers = {"Location": f"/tasks/{task.id}"}
    if not created:
        headers["Idempotent-Replayed"] = "true"
    log.info("task_enqueued", extra={"fields": {"task_id": task.id}})
    return JSONResponse({"task_id": task.id, "target_id": task.target_id}, status_code=202, headers=headers)
