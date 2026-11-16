from datetime import UTC, datetime, timedelta

import pytest

from minihci.config import Settings
from minihci.ha.health import next_health

T0 = datetime(2026, 1, 1, tzinfo=UTC)
S = Settings(heartbeat_suspect_misses=3, heartbeat_dead_after_s=30)


def at(s: float) -> datetime:
    return T0 + timedelta(seconds=s)


def test_defaults():
    s = Settings()
    assert (s.heartbeat_interval_s, s.heartbeat_ping_timeout_s) == (5, 3)
    assert (s.heartbeat_suspect_misses, s.heartbeat_dead_after_s) == (3, 30)


@pytest.mark.parametrize("status,missed", [("ready", 0), ("suspect", 4), ("dead", 9)])
def test_ok_always_goes_ready(status, missed):
    assert next_health(status, missed, at(-100), True, at(0), S) == ("ready", 0, at(0))


def test_ok_never_heartbeated():
    assert next_health("ready", 0, None, True, at(0), S) == ("ready", 0, at(0))


@pytest.mark.parametrize(
    "status,missed,last,now,expected",
    [
        ("ready", 0, at(0), at(5), ("ready", 1, at(0))),
        ("ready", 1, at(0), at(10), ("ready", 2, at(0))),
        ("ready", 2, at(0), at(15), ("suspect", 3, at(0))),
        ("suspect", 3, at(0), at(20), ("suspect", 4, at(0))),
        ("suspect", 5, at(0), at(29.9), ("suspect", 6, at(0))),
        ("suspect", 5, at(0), at(30), ("dead", 6, at(0))),
        ("ready", 0, at(0), at(31), ("dead", 1, at(0))),  # long gap: straight to dead
        ("dead", 20, at(0), at(35), ("dead", 21, at(0))),
        ("dead", 20, at(0), at(1000), ("dead", 21, at(0))),
    ],
)
def test_miss_transitions(status, missed, last, now, expected):
    assert next_health(status, missed, last, False, now, S) == expected


def test_never_heartbeated_clock_starts_at_first_miss():
    st, m, last = next_health("ready", 0, None, False, at(100), S)
    assert (st, m, last) == ("ready", 1, at(100))
    st, m, last = next_health(st, m, last, False, at(110), S)
    assert (st, m) == ("ready", 2)
    st, m, last = next_health(st, m, last, False, at(115), S)
    assert (st, m) == ("suspect", 3)
    st, m, last = next_health(st, m, last, False, at(130), S)
    assert (st, m, last) == ("dead", 4, at(100))


def test_suspect_threshold_configurable():
    s = Settings(heartbeat_suspect_misses=1)
    assert next_health("ready", 0, at(0), False, at(1), s)[0] == "suspect"
