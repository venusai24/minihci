from collections.abc import Iterator
from datetime import UTC

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily, HistogramMetricFamily, Metric
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from minihci.api import dr as dr_api
from minihci.models import HaRestart, Host, Migration, ProtectionPolicy, Replica, Task, Vm

router = APIRouter()

BUCKETS = (0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0)
MIGRATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0)
FINISHED_STATES = ("succeeded", "failed", "rolled_back")
HOST_STATUSES = ("registering", "ready", "suspect", "dead", "failed", "removing")


class DbCollector:
    def __init__(self, session_factory: sessionmaker) -> None:
        self._sf = session_factory

    def collect(self) -> Iterator[Metric]:
        with self._sf() as s:
            rows = s.execute(select(Task.type, Task.state, func.count()).group_by(Task.type, Task.state)).all()
        g = GaugeMetricFamily("minihci_tasks", "Tasks by type and state", labels=["type", "state"])
        for type_, state, n in rows:
            g.add_metric([type_, state], n)
        yield g
        yield self._task_durations()
        yield from self._hosts()
        yield from self._migrations()
        yield from self._ha()
        yield from self._replication()

    def _replication(self) -> Iterator[Metric]:
        now = dr_api._now()
        with self._sf() as s:
            pols = s.execute(
                select(ProtectionPolicy, Vm.name)
                .join(Vm, Vm.id == ProtectionPolicy.vm_id)
                .where(ProtectionPolicy.enabled.is_(True), Vm.desired_state != "absent")
                .order_by(Vm.name)
            ).all()
            statuses = [(name, dr_api.protection_status(s, p, now)) for p, name in pols]
            counts = s.execute(
                select(Replica.mode, Replica.status, func.count()).group_by(Replica.mode, Replica.status)
            ).all()
            sizes = s.execute(
                select(Replica.mode, func.coalesce(func.sum(Replica.size_bytes), 0))
                .where(Replica.status.in_(("succeeded", "pruned")))
                .group_by(Replica.mode)
            ).all()
        lag = GaugeMetricFamily(
            "minihci_replication_lag_seconds", "Seconds since the VM's newest replica", labels=["vm"]
        )
        within = GaugeMetricFamily(
            "minihci_replication_within_target", "1 when the VM meets its RPO target (D-036)", labels=["vm"]
        )
        for name, st in statuses:
            if st["lag_s"] is not None:  # at least one replica
                lag.add_metric([name], st["lag_s"])
                within.add_metric([name], 1.0 if st["within_target"] else 0.0)
        yield lag
        yield within
        reps = GaugeMetricFamily("minihci_replicas", "Replicas by mode and status", labels=["mode", "status"])
        for mode, status, n in counts:
            reps.add_metric([mode, status], n)
        yield reps
        by = GaugeMetricFamily("minihci_replica_bytes", "Bytes held by succeeded and pruned replicas", labels=["mode"])
        for mode, total in sizes:
            by.add_metric([mode], float(total))
        yield by

    def _ha(self) -> Iterator[Metric]:
        with self._sf() as s:
            pending = s.scalar(select(func.count()).select_from(Vm).where(Vm.actual_state == "pending_restart")) or 0
            rows = s.execute(
                select(Vm.id, Vm.name, HaRestart.recovery_ms)
                .join(Vm, Vm.id == HaRestart.vm_id)
                .where(
                    HaRestart.running_at.is_not(None),
                    HaRestart.recovery_ms.is_not(None),
                    Vm.desired_state != "absent",
                )
                .order_by(Vm.id, HaRestart.running_at, HaRestart.id)
            ).all()
        g = GaugeMetricFamily("minihci_vms_pending_restart", "VMs waiting for capacity to be restarted")
        g.add_metric([], pending)
        yield g
        with self._sf() as s:
            unhealthy = (
                s.scalar(
                    select(func.count())
                    .select_from(Vm)
                    .where(Vm.actual_state == "paused", Vm.state_reason == "ioerror", Vm.desired_state != "absent")
                )
                or 0
            )
        u = GaugeMetricFamily("minihci_vms_unhealthy", "VMs paused by a disk I/O error (D-026)")
        u.add_metric([], unhealthy)
        yield u
        last = {vm_id: (name, ms) for vm_id, name, ms in rows}  # ordered: last restart per VM wins
        r = GaugeMetricFamily(
            "minihci_ha_restart_recovery_seconds",
            "Failure-to-running time of each VM's last HA restart",
            labels=["vm"],
        )
        for name, ms in sorted(last.values()):
            r.add_metric([name], ms / 1000.0)
        yield r

    def _migrations(self) -> Iterator[Metric]:
        with self._sf() as s:
            cols = {
                "minihci_migration_duration_seconds": Migration.duration_ms,
                "minihci_migration_downtime_seconds": Migration.downtime_ms,
            }
            data = {
                name: [
                    r[0] / 1000.0
                    for r in s.execute(select(col).where(Migration.status == "succeeded", col.is_not(None))).all()
                ]
                for name, col in cols.items()
            }
        for name, doc in (
            ("minihci_migration_duration_seconds", "Migration duration in seconds"),
            ("minihci_migration_downtime_seconds", "Migration downtime in seconds"),
        ):
            vals = data[name]
            h = HistogramMetricFamily(name, doc)
            buckets = [(str(float(b)), float(sum(v <= b for v in vals))) for b in MIGRATION_BUCKETS]
            buckets.append(("+Inf", float(len(vals))))
            h.add_metric([], buckets, sum(vals))
            yield h

    def _hosts(self) -> Iterator[Metric]:
        with self._sf() as s:
            rows = s.execute(
                select(Host.name, Host.status, Host.last_heartbeat, Host.missed_heartbeats)
                .where(Host.status != "removed")
                .order_by(Host.name)
            ).all()
        st = GaugeMetricFamily("minihci_host_status", "Host status (one-hot)", labels=["host", "status"])
        hb = GaugeMetricFamily(
            "minihci_host_last_heartbeat_timestamp_seconds", "Last host heartbeat (unix time)", labels=["host"]
        )
        missed = GaugeMetricFamily("minihci_host_missed_heartbeats", "Consecutive missed heartbeats", labels=["host"])
        for name, status, beat, misses in rows:
            missed.add_metric([name], misses)
            for known in HOST_STATUSES:
                st.add_metric([name, known], 1.0 if status == known else 0.0)
            if beat is not None:
                if beat.tzinfo is None:
                    beat = beat.replace(tzinfo=UTC)
                hb.add_metric([name], beat.timestamp())
        yield st
        yield hb
        yield missed

    def _task_durations(self) -> HistogramMetricFamily:
        with self._sf() as s:
            rows = s.execute(
                select(Task.type, Task.started_at, Task.finished_at).where(
                    Task.state.in_(FINISHED_STATES), Task.started_at.is_not(None), Task.finished_at.is_not(None)
                )
            ).all()
        by_type: dict[str, list[float]] = {}
        for type_, started, finished in rows:
            by_type.setdefault(type_, []).append((finished - started).total_seconds())
        h = HistogramMetricFamily("minihci_task_duration_seconds", "Task duration in seconds", labels=["type"])
        for type_, ds in sorted(by_type.items()):
            buckets = [(str(float(b)), float(sum(d <= b for d in ds))) for b in BUCKETS]
            buckets.append(("+Inf", float(len(ds))))
            h.add_metric([type_], buckets, sum(ds))
        return h


@router.get("/metrics")
def metrics(request: Request) -> Response:
    registry = CollectorRegistry()
    registry.register(DbCollector(request.app.state.session_factory))
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
