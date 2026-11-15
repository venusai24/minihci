from collections.abc import Iterator

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily, HistogramMetricFamily, Metric
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from minihci.models import Task

router = APIRouter()

BUCKETS = (0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0)
FINISHED_STATES = ("succeeded", "failed", "rolled_back")


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
