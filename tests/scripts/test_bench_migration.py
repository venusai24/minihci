import os
import py_compile
import subprocess
import sys
from pathlib import Path

from scripts import bench_migration as bm

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bench_migration.py"


def _env():
    env = {k: v for k, v in os.environ.items() if k != "MINIHCI_REAL_HOSTS"}
    env["PYTHONPATH"] = str(ROOT)
    return env


def test_refuses_without_env_and_writes_no_file(tmp_path):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--out-dir", str(tmp_path), "--image", "x"],
        check=False,
        capture_output=True,
        text=True,
        env=_env(),
        cwd=tmp_path,
    )
    assert r.returncode != 0
    assert "MINIHCI_REAL_HOSTS" in r.stderr
    assert list(tmp_path.iterdir()) == []


def test_py_compile():
    py_compile.compile(str(SCRIPT), doraise=True)


def test_other_host():
    assert bm.other_host(1, [1, 2]) == 2
    assert bm.other_host(2, [1, 2]) == 1


def test_build_rows_failed_not_timed():
    results = [(11, "succeeded"), (12, "failed"), (13, "succeeded")]
    migs = [
        {
            "task_id": 11,
            "src_host_id": 1,
            "dst_host_id": 2,
            "status": "succeeded",
            "duration_ms": 900,
            "downtime_ms": 40,
        },
        {"task_id": 12, "src_host_id": 2, "dst_host_id": 1, "status": "failed", "duration_ms": 5, "downtime_ms": None},
    ]
    rows, durations, downtimes = bm.build_rows(results, migs)
    assert [r["round"] for r in rows] == [1, 2, 3]
    assert rows[1]["task_state"] == "failed"
    assert rows[1]["duration_ms"] is None
    assert rows[2]["duration_ms"] is None  # no migrations row: nothing invented
    assert durations == [900]
    assert downtimes == [40]


def test_output_path():
    assert bm.output_path(Path("d"), "2026-11-17") == Path("d/migration-2026-11-17.md")
