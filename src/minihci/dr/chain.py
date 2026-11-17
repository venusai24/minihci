"""Pure replica chain, retention and RPO rules (D-034, D-035, D-036).

No DB, driver or I/O imports: everything here is a function of its arguments.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

RPO_SLACK_S = 30
RPO_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class ReplicaInfo:
    id: int
    chain_id: int
    seq: int
    dr_path: str
    checkpoint: str
    snapshot_time: datetime
    finished_at: datetime


@dataclass(frozen=True)
class NextReplica:
    mode: Literal["full", "incremental"]
    since: str | None
    backing: str | None
    chain_id: int | None
    seq: int
    # Why it is full: "first" | "checkpoint_missing" | "backing_missing" |
    # "chain_full"; None for incremental.
    reason: str | None


@dataclass(frozen=True)
class RpoStatus:
    last_snapshot_at: datetime | None
    lag_s: float | None
    achieved_rpo_s: float | None
    target_s: int
    within_target: bool


def _by_age(replicas: list[ReplicaInfo]) -> list[ReplicaInfo]:
    """Oldest first; ties broken by id so ordering is deterministic."""
    return sorted(replicas, key=lambda r: (r.snapshot_time, r.id))


def _full(reason: str) -> NextReplica:
    return NextReplica(
        mode="full", since=None, backing=None, chain_id=None, seq=0, reason=reason
    )


def next_replica(
    succeeded: list[ReplicaInfo],
    retention: int,
    host_checkpoints: list[str],
    last_file_present: bool,
) -> NextReplica:
    if not succeeded:
        return _full("first")
    last = _by_age(succeeded)[-1]
    if last.checkpoint not in host_checkpoints:
        return _full("checkpoint_missing")
    if not last_file_present:
        return _full("backing_missing")
    if last.seq + 1 >= retention:
        return _full("chain_full")
    return NextReplica(
        mode="incremental",
        since=last.checkpoint,
        backing=last.dr_path,
        chain_id=last.chain_id,
        seq=last.seq + 1,
        reason=None,
    )


def prune_ids(succeeded: list[ReplicaInfo], retention: int) -> list[int]:
    """Ids safe to delete: keep the newest `retention` plus their backing chains.

    Returned grouped by chain, highest seq first, so children go before parents.
    """
    if retention < 1:
        retention = 1
    newest = _by_age(succeeded)[::-1][:retention]
    keep: set[int] = {r.id for r in newest}
    for k in newest:
        for r in succeeded:
            if r.chain_id == k.chain_id and r.seq < k.seq:
                keep.add(r.id)
    doomed = [r for r in succeeded if r.id not in keep]
    doomed.sort(key=lambda r: (r.chain_id, -r.seq, r.id))
    return [r.id for r in doomed]


def rpo_status(history: list[ReplicaInfo], interval_s: int, now: datetime) -> RpoStatus:
    """history = succeeded and pruned replicas inside RPO_WINDOW."""
    target = interval_s + RPO_SLACK_S
    if not history:
        return RpoStatus(None, None, None, target, False)
    ordered = _by_age(history)
    newest = ordered[-1]
    lag = (now - newest.snapshot_time).total_seconds()
    achieved: float | None = None
    if len(ordered) >= 2:
        achieved = max(
            (b.finished_at - a.snapshot_time).total_seconds()
            for a, b in zip(ordered, ordered[1:])
        )
    return RpoStatus(
        last_snapshot_at=newest.snapshot_time,
        lag_s=lag,
        achieved_rpo_s=achieved,
        target_s=target,
        within_target=max(achieved or 0, lag) <= target,
    )
