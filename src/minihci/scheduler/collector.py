"""Host metrics collector (FR-3.3, D-023). Observation only: reads host stats, never mutates hosts or tasks."""

import logging
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from minihci.config import Settings
from minihci.driver.base import DriverError, HostDriver
from minihci.models import Host, HostMetric
from minihci.scheduler.placement import PLACEABLE_STATUSES

log = logging.getLogger("minihci.collector")


@dataclass
class CollectReport:
    collected: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    pruned: int = 0


def collect_once(session_factory: sessionmaker[Session], driver: HostDriver, settings: Settings) -> CollectReport:
    report = CollectReport()
    with session_factory() as s:
        hosts = [
            (h.id, h.libvirt_uri)
            for h in s.scalars(select(Host).where(Host.status.in_(PLACEABLE_STATUSES)).order_by(Host.id))
        ]
    samples = []
    for host_id, uri in hosts:
        try:
            stats = driver.host_stats(uri)
        except DriverError as e:
            report.failed.append(host_id)
            log.warning(
                "host stats failed",
                extra={"fields": {"event": "host_stats_failed", "host_id": host_id, "error": str(e)}},
            )
            continue
        samples.append((host_id, stats))
    with session_factory() as s, s.begin():
        for host_id, stats in samples:
            s.add(HostMetric(host_id=host_id, cpu_pct=stats.cpu_pct, mem_used_mb=stats.mem_used_mb))
            report.collected.append(host_id)
        res = s.execute(
            text("DELETE FROM host_metrics WHERE collected_at < now() - make_interval(secs => :secs)"),
            {"secs": settings.metrics_retention_h * 3600},
        )
        report.pruned = res.rowcount
    return report
