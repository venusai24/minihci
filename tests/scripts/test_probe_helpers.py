"""Pure helpers behind scripts/probe_dr.py: retry-with-timing and per-run file names."""

import pytest

from scripts import benchlib


class _Clock:
    """Injected clock: sleep() advances now()."""

    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_retry_until_returns_on_first_success_with_zero_wait():
    c = _Clock()
    result, waited = benchlib.retry_until(lambda: "ok", 40, 2, sleep=c.sleep, now=c.now)
    assert (result, waited) == ("ok", 0.0)


def test_retry_until_retries_then_succeeds_and_reports_wait():
    c = _Clock()
    calls = []

    def flaky():
        calls.append(c.now())
        if len(calls) < 4:
            raise OSError("Resource temporarily unavailable")
        return "ok"

    result, waited = benchlib.retry_until(flaky, 40, 2, sleep=c.sleep, now=c.now, retry_on=(OSError,))
    assert result == "ok"
    assert waited == 6.0
    assert calls == [0.0, 2.0, 4.0, 6.0]


def test_retry_until_gives_up_at_the_deadline_and_raises_the_last_error():
    c = _Clock()
    n = []

    def always():
        n.append(1)
        raise OSError(f"attempt {len(n)}")

    with pytest.raises(OSError) as e:
        benchlib.retry_until(always, 10, 3, sleep=c.sleep, now=c.now, retry_on=(OSError,))
    assert c.t <= 10 + 3  # bounded: never sleeps past the deadline by more than one interval
    assert str(e.value) == f"attempt {len(n)}"  # the LAST error, not the first


def test_retry_until_does_not_retry_other_errors():
    c = _Clock()

    def boom():
        raise ValueError("x")

    with pytest.raises(ValueError):
        benchlib.retry_until(boom, 40, 2, sleep=c.sleep, now=c.now, retry_on=(OSError,))
    assert c.t == 0.0


def test_probe_paths_are_unique_per_run_and_share_one_prefix():
    a = benchlib.probe_paths("/srv/minihci/dr", "mhci-dr-probe", "r1")
    b = benchlib.probe_paths("/srv/minihci/dr", "mhci-dr-probe", "r2")
    assert set(a) == {"full", "inc", "blank_q5", "blank_q6", "boot"}
    assert all(p.startswith("/srv/minihci/dr/mhci-dr-probe-r1-") and p.endswith(".qcow2") for p in a.values())
    assert not set(a.values()) & set(b.values())  # no name is ever reused across runs
    assert len(set(a.values())) == 5
