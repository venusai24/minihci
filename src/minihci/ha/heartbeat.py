"""Host heartbeats (FR-4.1, D-027). Pings hosts in parallel and applies the pure `next_health`."""

import logging
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from time import monotonic

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from minihci.config import Settings
from minihci.driver.base import HostDriver
from minihci.ha.failover import failover_once
from minihci.ha.health import next_health
from minihci.models import Host, Task
from minihci.tasks.engine import enqueue

log = logging.getLogger("minihci.heartbeat")

WATCHED = ("ready", "suspect", "dead")


def _ping(driver: HostDriver, uri: str, timeout: float) -> bool:
    try:
        return bool(driver.ping(uri, timeout))
    except Exception:  # noqa: BLE001 - any ping failure is a miss
        return False


def _apply(session_factory: sessionmaker[Session], settings: Settings, host_id: int, ok: bool, now: datetime) -> None:
    with session_factory() as s, s.begin():
        h = s.scalars(select(Host).where(Host.id == host_id).with_for_update()).one_or_none()
        if h is None or h.status not in WATCHED:
            return
        old = h.status
        # hosts.last_heartbeat is a naive-UTC column: convert at the boundary
        last = h.last_heartbeat.replace(tzinfo=UTC) if h.last_heartbeat is not None else None
        h.status, h.missed_heartbeats, new_last = next_health(h.status, h.missed_heartbeats, last, ok, now, settings)
        h.last_heartbeat = new_last.astimezone(UTC).replace(tzinfo=None) if new_last is not None else None
        if ok and old == "dead":
            # D-028 (5): stays dead until host.reclaim has removed stale copies (it sets ready)
            h.status = "dead"
            open_task = s.scalars(
                select(Task.id).where(
                    Task.type == "host.reclaim", Task.target_id == host_id, Task.state.in_(("pending", "running"))
                )
            ).first()
            if open_task is None:
                task = enqueue(s, "host.reclaim", host_id, {})
                log.warning(
                    "host reclaim enqueued",
                    extra={"fields": {"event": "host_reclaim_enqueued", "host_id": host_id, "task_id": task.id}},
                )
            return
        if h.status != old:
            log.warning(
                "host health changed",
                extra={
                    "fields": {
                        "event": "host_health_changed",
                        "host_id": host_id,
                        "from": old,
                        "to": h.status,
                        "missed": h.missed_heartbeats,
                    }
                },
            )


def heartbeat_once(
    session_factory: sessionmaker[Session], driver: HostDriver, settings: Settings, now: datetime | None = None
) -> None:
    with session_factory() as s:
        hosts = [
            (h.id, h.libvirt_uri) for h in s.scalars(select(Host).where(Host.status.in_(WATCHED)).order_by(Host.id))
        ]
    if not hosts:
        return
    now = now or datetime.now(UTC)
    timeout = settings.heartbeat_ping_timeout_s
    pool = ThreadPoolExecutor(max_workers=len(hosts), thread_name_prefix="ping")
    try:
        pending: dict[Future[bool], int] = {pool.submit(_ping, driver, uri, timeout): hid for hid, uri in hosts}
        deadline = monotonic() + timeout + 0.5  # grace over the driver's own timeout
        # Apply each result as soon as it arrives, so a hung host never delays the others.
        while pending:
            left = deadline - monotonic()
            if left <= 0:
                break
            done, _ = wait(pending, timeout=left, return_when=FIRST_COMPLETED)
            for f in done:
                _apply(session_factory, settings, pending.pop(f), f.result(), now)
        for hid in pending.values():  # still hung: counts as a miss
            _apply(session_factory, settings, hid, False, now)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def run_forever(stop: threading.Event, session_factory, driver: HostDriver, settings: Settings) -> None:
    while not stop.is_set():
        try:
            heartbeat_once(session_factory, driver, settings)
        except Exception:
            log.exception("heartbeat pass failed")
        try:
            failover_once(session_factory, settings)
        except Exception:
            log.exception("failover pass failed")
        stop.wait(settings.heartbeat_interval_s)
