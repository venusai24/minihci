import io
import json
import logging

import pytest
from sqlalchemy import select, text

from minihci.config import Settings
from minihci.driver.fake import FakeDriver
from minihci.logging import configure_logging
from minihci.models import Host, HostMetric
from minihci.scheduler.collector import collect_once
from tests.helpers import add_ready_host

U1 = "qemu+ssh://a/system"
U2 = "qemu+ssh://b/system"


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def hosts(session_factory, driver):
    h1 = add_ready_host(session_factory, driver, "a", U1)
    h2 = add_ready_host(session_factory, driver, "b", U2)
    return h1, h2


def rows(session_factory, host_id=None):
    with session_factory() as s:
        q = select(HostMetric).order_by(HostMetric.id)
        if host_id is not None:
            q = q.where(HostMetric.host_id == host_id)
        return s.scalars(q).all()


def test_defaults():
    assert Settings().collector_interval_s == 10
    assert Settings().metrics_retention_h == 24


def test_collects_loads(session_factory, driver, hosts):
    h1, h2 = hosts
    driver.set_load(U1, 90)
    driver.set_load(U2, 10)
    report = collect_once(session_factory, driver, Settings())
    assert sorted(report.collected) == [h1, h2] and report.failed == []
    assert [r.cpu_pct for r in rows(session_factory, h1)] == [90]
    assert [r.cpu_pct for r in rows(session_factory, h2)] == [10]
    assert rows(session_factory, h1)[0].collected_at is not None
    collect_once(session_factory, driver, Settings())
    assert len(rows(session_factory, h1)) == 2 and len(rows(session_factory, h2)) == 2


def test_unreachable_host_failed_others_collected(session_factory, driver, hosts):
    h1, h2 = hosts
    with session_factory() as s:
        ghost = Host(name="ghost", libvirt_uri="qemu+ssh://ghost/system", status="ready", cpu_total=1, mem_total_mb=1)
        s.add(ghost)
        s.commit()
        gid = ghost.id
    buf = io.StringIO()
    configure_logging("INFO", buf)
    try:
        report = collect_once(session_factory, driver, Settings())
    finally:
        logging.getLogger("minihci").handlers.clear()
    assert report.failed == [gid] and sorted(report.collected) == [h1, h2]
    assert rows(session_factory, gid) == []
    evs = [json.loads(line) for line in buf.getvalue().splitlines() if line.startswith("{")]
    assert any(e.get("event") == "host_stats_failed" and e.get("host_id") == gid for e in evs)


def test_non_placeable_hosts_skipped(session_factory, driver, hosts):
    h1, h2 = hosts
    with session_factory() as s, s.begin():
        s.get(Host, h1).status = "removed"
        s.get(Host, h2).status = "registering"
    report = collect_once(session_factory, driver, Settings())
    assert report.collected == [] and report.failed == []
    assert rows(session_factory) == []


def test_prunes_old_rows(session_factory, driver, hosts):
    h1, _ = hosts
    with session_factory() as s, s.begin():
        for hours in (25, 23):
            s.execute(
                text(
                    "INSERT INTO host_metrics (host_id, collected_at, cpu_pct, mem_used_mb) "
                    "VALUES (:h, now() - make_interval(hours => :n), 5, 5)"
                ),
                {"h": h1, "n": hours},
            )
    report = collect_once(session_factory, driver, Settings())
    assert report.pruned == 1
    assert len(rows(session_factory, h1)) == 2  # 23h row + new one


def test_no_mutating_driver_calls(session_factory, hosts):
    d = FakeDriver(fail={m: 5 for m in ("start_domain", "destroy_domain", "define_domain", "shutdown_domain")})
    d.add_host(U1, 8, 16384)
    d.add_host(U2, 8, 16384)
    report = collect_once(session_factory, d, Settings())
    assert len(report.collected) == 2
    assert all(v == 5 for v in d.fail.values())
