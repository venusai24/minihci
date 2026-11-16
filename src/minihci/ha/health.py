"""Pure host-health state machine (FR-4.1, D-027). `ready` is the PRD's "up"."""

from datetime import datetime

from minihci.config import Settings


def next_health(
    status: str,
    missed: int,
    last_heartbeat: datetime | None,
    ok: bool,
    now: datetime,
    settings: Settings,
) -> tuple[str, int, datetime | None]:
    """Return (status, missed, last_heartbeat) after one ping result.

    A host that never had a heartbeat starts its dead clock at its first miss;
    that time is stored in last_heartbeat as the baseline.
    """
    if ok:
        return "ready", 0, now
    missed += 1
    if last_heartbeat is None:
        last_heartbeat = now
    if status == "dead" or (now - last_heartbeat).total_seconds() >= settings.heartbeat_dead_after_s:
        return "dead", missed, last_heartbeat
    if missed >= settings.heartbeat_suspect_misses:
        return "suspect", missed, last_heartbeat
    return status if status in ("ready", "suspect") else "ready", missed, last_heartbeat
