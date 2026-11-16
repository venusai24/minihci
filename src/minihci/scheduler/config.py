"""Scheduler configuration loading (FR-3.2, FR-3.4, D-022, D-025)."""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from minihci.models import SchedulerConfig

DEFAULT_CPU_HEADROOM_PCT = 80

# Config field names stored in the scheduler_config row.
REBALANCE_FIELDS = (
    "rebalance_enabled",
    "rebalance_cpu_threshold_pct",
    "rebalance_window_s",
    "rebalance_vm_cooldown_s",
    "rebalance_host_cooldown_s",
)
CONFIG_FIELDS = ("cpu_headroom_pct", *REBALANCE_FIELDS)


@dataclass(frozen=True)
class Config:
    cpu_headroom_pct: int = DEFAULT_CPU_HEADROOM_PCT
    rebalance_enabled: bool = False
    rebalance_cpu_threshold_pct: int = 80
    rebalance_window_s: int = 300
    rebalance_vm_cooldown_s: int = 1800
    rebalance_host_cooldown_s: int = 600


def load_config(session: Session) -> Config:
    """Read the single config row; fall back to defaults if it is missing."""
    row = session.get(SchedulerConfig, 1)
    if row is None:
        return Config()
    return Config(**{f: getattr(row, f) for f in CONFIG_FIELDS})
