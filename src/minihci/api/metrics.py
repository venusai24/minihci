from collections.abc import Iterator

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily, Metric
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from minihci.models import Task

router = APIRouter()


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


@router.get("/metrics")
def metrics(request: Request) -> Response:
    registry = CollectorRegistry()
    registry.register(DbCollector(request.app.state.session_factory))
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
