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
