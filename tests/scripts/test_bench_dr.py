import os
import subprocess
import sys
from pathlib import Path

from scripts import acceptance_m5, benchlib

ROOT = Path(__file__).resolve().parents[2]
NONE_STATS = {"median": None, "worst": None}


def _rows():
    return [
        {"mode": "full", "status": "succeeded", "size_bytes": 1000, "duration_ms": 900},
        {"mode": "incremental", "status": "succeeded", "size_bytes": 10, "duration_ms": 100},
        {"mode": "incremental", "status": "succeeded", "size_bytes": 30, "duration_ms": 300},
        {"mode": "incremental", "status": "succeeded", "size_bytes": 20, "duration_ms": 200},
        {"mode": "incremental", "status": "failed", "size_bytes": 999999, "duration_ms": 9},
        {"mode": "full", "status": "pruned", "size_bytes": 3000, "duration_ms": 1100},
    ]


def test_summarise_replicas_separates_modes_median_worst():
    s = benchlib.summarise_replicas(_rows())
    assert s["incremental"] == {
        "count": 3,
        "size_bytes": {"median": 20, "worst": 30},
        "duration_ms": {"median": 200, "worst": 300},
    }
    assert s["full"]["count"] == 2  # succeeded and pruned count, failed does not
    assert s["full"]["size_bytes"] == {"median": 2000, "worst": 3000}


def test_summarise_replicas_empty_is_none():
    s = benchlib.summarise_replicas([])
    assert s["full"] == {"count": 0, "size_bytes": NONE_STATS, "duration_ms": NONE_STATS}


def test_summarise_replicas_ignores_null_sizes():
    s = benchlib.summarise_replicas([{"mode": "full", "status": "succeeded", "size_bytes": None, "duration_ms": 5}])
    assert s["full"]["size_bytes"]["median"] is None
    assert s["full"]["duration_ms"]["median"] == 5


def test_verdict_pass_and_fail():
    assert acceptance_m5.verdict(1000.0, 950.0, 60) == (50.0, True)
    assert acceptance_m5.verdict(1000.0, 900.0, 60) == (100.0, False)
    assert acceptance_m5.verdict(1000.0, 910.0, 60) == (90.0, True)  # boundary interval+30


def test_restart_lines_exact_empty_and_partial():
    assert acceptance_m5.restart_lines(["host1", "host2"]) == [
        "To bring the primary site back, run:",
        "virsh -c qemu:///system start host1",
        "virsh -c qemu:///system start host2",
    ]
    assert acceptance_m5.restart_lines([]) == ["No host VM was destroyed; nothing to restart."]
    partial = acceptance_m5.restart_lines(["host1", "host2"], uncertain="host2")
    assert "virsh -c qemu:///system start host2" in partial
    assert "host2" in partial[-1] and "unknown" in partial[-1]


def _run(names, destroy=None, wait_dead=None, after=None):
    out: list[str] = []

    def noop(*a):
        return None

    try:
        acceptance_m5.run_site_loss(names, destroy or noop, wait_dead or noop, after or noop, out.append)
        err = None
    except (RuntimeError, SystemExit) as e:
        err = e
    return out, err


def test_restart_printed_on_success():
    out, err = _run(["host1", "host2"])
    assert err is None
    assert out[-2:] == ["virsh -c qemu:///system start host1", "virsh -c qemu:///system start host2"]


def test_restart_printed_when_second_destroy_fails():
    def destroy(n):
        if n == "host2":
            raise RuntimeError("boom")

    out, err = _run(["host1", "host2", "host3"], destroy=destroy)
    assert isinstance(err, RuntimeError)
    assert "virsh -c qemu:///system start host1" in out
    assert "virsh -c qemu:///system start host2" in out
    assert not any("host3" in line for line in out)
    assert "unknown" in out[-1]


def test_restart_printed_when_wait_or_failover_fails():
    def bad():
        raise SystemExit("nope")

    for kw in ({"wait_dead": bad}, {"after": bad}):
        out, err = _run(["host1"], **kw)
        assert isinstance(err, SystemExit)
        assert out[-1] == "virsh -c qemu:///system start host1"


def test_wait_all_dead_uses_own_deadline_and_times_out():
    clock = [1000.0]

    def sleep(s):
        clock[0] += s

    calls = []
    ok = acceptance_m5.wait_all_dead(lambda: calls.append(1) or False, 10, sleep=sleep, now=lambda: clock[0])
    assert ok is False and len(calls) > 2
    clock[0] = 5000.0  # time spent elsewhere does not matter: the deadline starts at the call
    n = []
    assert acceptance_m5.wait_all_dead(lambda: len(n.append(1) or n) >= 3, 10, sleep=sleep, now=lambda: clock[0])


def test_scripts_refuse_without_env(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "MINIHCI_REAL_HOSTS"}
    env["PYTHONPATH"] = str(ROOT)
    for script, args in (
        ("bench_dr.py", ["--vm", "1"]),
        ("acceptance_m5.py", ["--vm", "1", "--hosts", "host1"]),
    ):
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            cwd=tmp_path,
        )
        assert r.returncode == 2, script
        assert "MINIHCI_REAL_HOSTS" in r.stderr
