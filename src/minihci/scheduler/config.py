"""Scheduler configuration loading (FR-3.2, D-022)."""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from minihci.models import SchedulerConfig

DEFAULT_CPU_HEADROOM_PCT = 80


@dataclass(frozen=True)
class Config:
    cpu_headroom_pct: int = DEFAULT_CPU_HEADROOM_PCT


def load_config(session: Session) -> Config:
    """Read the single config row; fall back to defaults if it is missing."""
    row = session.get(SchedulerConfig, 1)
    if row is None:
        return Config()
    return Config(cpu_headroom_pct=row.cpu_headroom_pct)
