"""Pure helpers for the libvirt driver (no libvirt import). See D-026."""

import logging
from collections.abc import Mapping

from minihci.driver.base import DomainState, MigrationResult

log = logging.getLogger(__name__)

# virDomainState: nostate, running, blocked, paused, shutdown, shutoff, crashed, pmsuspended
_RUNNING_CODES = {1, 2, 3, 4, 7}


def _busy_idle(sample: Mapping[str, int]) -> tuple[int, int]:
    # "guest" time is already counted inside "user", so it is not added again.
    idle = sample.get("idle", 0) + sample.get("iowait", 0)
    return sample.get("user", 0) + sample.get("kernel", 0), idle


def cpu_pct(prev: Mapping[str, int], cur: Mapping[str, int]) -> float:
    pb, pi = _busy_idle(prev)
    cb, ci = _busy_idle(cur)
    d_total = (cb + ci) - (pb + pi)
    if d_total <= 0:
        return 0.0
    pct = 100.0 * (1 - (ci - pi) / d_total)
    return max(0.0, min(100.0, pct))


def mem_used_mb(kib_stats: Mapping[str, int]) -> int:
    used = kib_stats["total"] - kib_stats["free"] - kib_stats.get("buffers", 0) - kib_stats.get("cached", 0)
    return max(0, used // 1024)


def map_domain_state(code: int) -> DomainState:
    return DomainState.RUNNING if code in _RUNNING_CODES else DomainState.SHUT_OFF


def migration_result(job_stats: Mapping[str, int] | None, wall_ms: int) -> MigrationResult:
    stats = job_stats or {}
    downtime = stats.get("downtime")
    if downtime is None:
        log.warning("migration job stats have no 'downtime'; recording None")
    elapsed = stats.get("time_elapsed")
    if elapsed is None:
        log.warning("migration job stats have no 'time_elapsed'; using wall clock")
        elapsed = wall_ms
    return MigrationResult(duration_ms=int(elapsed), downtime_ms=None if downtime is None else int(downtime))
