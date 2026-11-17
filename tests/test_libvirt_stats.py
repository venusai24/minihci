import logging

import pytest

from minihci.driver.base import DomainState, MigrationResult
from minihci.driver.libvirt_stats import cpu_pct, map_domain_state, mem_used_mb, migration_result


def s(user=0, kernel=0, idle=0, iowait=0, guest=0):
    return {"user": user, "kernel": kernel, "idle": idle, "iowait": iowait, "guest": guest}


@pytest.mark.parametrize(
    "prev,cur,want",
    [
        (s(), s(user=50, idle=50), 50.0),
        (s(), s(idle=100), 0.0),
        (s(), s(user=100), 100.0),
        (s(), s(user=25, kernel=25, idle=25, iowait=25), 50.0),
        (s(user=10, idle=10), s(user=10, idle=10), 0.0),  # delta total 0
        (s(user=10, idle=10), s(user=5, idle=5), 0.0),  # counters went backwards
        (s(), s(user=50, idle=50, guest=1000), 50.0),  # guest is inside user
    ],
)
def test_cpu_pct(prev, cur, want):
    assert cpu_pct(prev, cur) == pytest.approx(want)


def test_cpu_pct_clamped():
    assert cpu_pct(s(), s(user=10, idle=-5)) == 100.0


def test_mem_used_mb():
    st = {"total": 2048 * 1024, "free": 1000 * 1024, "buffers": 48 * 1024, "cached": 500 * 1024}
    assert mem_used_mb(st) == 500
    assert mem_used_mb({"total": 10, "free": 100, "buffers": 0, "cached": 0}) == 0


@pytest.mark.parametrize(
    "code,want",
    [
        (0, DomainState.SHUT_OFF),
        (1, DomainState.RUNNING),
        (2, DomainState.RUNNING),
        (3, DomainState.PAUSED),
        (4, DomainState.RUNNING),
        (5, DomainState.SHUT_OFF),
        (6, DomainState.CRASHED),
        (7, DomainState.RUNNING),
    ],
)
def test_map_domain_state(code, want):
    assert map_domain_state(code) is want


def test_migration_result_full():
    r = migration_result({"downtime": 88, "time_elapsed": 696}, 1050)
    assert r == MigrationResult(duration_ms=696, downtime_ms=88)


def test_migration_result_missing_keys(caplog):
    with caplog.at_level(logging.WARNING):
        r = migration_result({"time_elapsed": 700}, 1050)
    assert r == MigrationResult(duration_ms=700, downtime_ms=None)
    assert caplog.records
    r = migration_result(None, 1050)
    assert r == MigrationResult(duration_ms=1050, downtime_ms=None)
