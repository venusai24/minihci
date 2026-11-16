"""Scheduler configuration API (FR-3.2, D-022)."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from minihci.api.deps import get_session
from minihci.scheduler.config import load_config

router = APIRouter()


@router.get("/scheduler/config")
def get_scheduler_config(session: Session = Depends(get_session)) -> dict[str, int]:
    return {"cpu_headroom_pct": load_config(session).cpu_headroom_pct}
