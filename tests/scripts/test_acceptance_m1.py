import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "acceptance_m1.sh"


def test_bash_syntax_ok():
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr


def test_refuses_without_env():
    env = {k: v for k, v in os.environ.items() if k != "MINIHCI_REAL_HOSTS"}
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, check=False, env=env)
    assert r.returncode != 0
    assert "MINIHCI_REAL_HOSTS" in r.stderr
