import os
import py_compile
import subprocess
import sys
from pathlib import Path

from scripts import bench_ha as bh

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "bench_ha.py"


def _env():
    env = {k: v for k, v in os.environ.items() if k != "MINIHCI_REAL_HOSTS"}
    env["PYTHONPATH"] = str(ROOT)
    return env


def test_refuses_without_env_and_writes_no_file(tmp_path):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--out-dir", str(tmp_path), "--host", "host1"],
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


def _vm(i, host, state):
    return {"id": i, "host_id": host, "actual_state": state}


def test_ha_elapsed_first_time_all_recovered():
    obs = [
        (101.0, [_vm(1, 1, "running"), _vm(2, 1, "running")]),
        (103.0, [_vm(1, 2, "running"), _vm(2, 1, "running")]),
        (105.5, [_vm(1, 2, "running"), _vm(2, 2, "running")]),
        (106.0, [_vm(1, 2, "running"), _vm(2, 2, "running")]),
    ]
    assert bh.ha_elapsed(100.0, obs, {1, 2}, dead_host_id=1) == 5.5


def test_ha_elapsed_requires_running_and_elsewhere():
    obs = [(101.0, [_vm(1, 2, "pending_restart")]), (102.0, [_vm(1, 2, "running")])]
    assert bh.ha_elapsed(100.0, obs, {1}, dead_host_id=1) == 2.0


def test_ha_elapsed_never_recovered_is_none():
    obs = [(101.0, [_vm(1, 1, "running")])]
    assert bh.ha_elapsed(100.0, obs, {1}, dead_host_id=1) is None
    assert bh.ha_elapsed(100.0, [], {1}, dead_host_id=1) is None
    assert bh.ha_elapsed(100.0, obs, set(), dead_host_id=1) is None


def test_output_path():
    assert bh.output_path(Path("d"), "2026-11-17") == Path("d/ha-2026-11-17.md")
