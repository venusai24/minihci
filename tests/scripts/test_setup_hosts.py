import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "setup-hosts.sh"


def test_bash_syntax_ok():
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr


def test_refuses_without_env_and_runs_nothing(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("sudo", "ssh", "virsh", "virt-install", "virt-customize", "qemu-img"):
        stub = bin_dir / name
        stub.write_text('#!/bin/sh\ntouch "$MARKER"\nexit 0\n')
        stub.chmod(0o755)
    marker = tmp_path / "ran"
    env = {k: v for k, v in os.environ.items() if k not in ("MINIHCI_REAL_HOSTS", "FEDORA_IMAGE", "SSH_PUBKEY")}
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["MARKER"] = str(marker)
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, check=False, env=env)
    assert r.returncode == 2
    assert "MINIHCI_REAL_HOSTS" in r.stderr
    assert not marker.exists()


def test_review_header_present():
    head = "\n".join(SCRIPT.read_text().splitlines()[:15]).lower()
    assert "owner must review" in head


def test_nfs_lease_dropin_and_fence_comment():
    text = SCRIPT.read_text()
    assert "lease-time=15" in text
    assert "grace-time=15" in text
    assert "/etc/nfs.conf.d/minihci.conf" in text
    assert "HA disk fence" in text
    assert "52:54:00:4d:48:1" in text
    assert "virt_use_nfs" in text
    assert "49152-49215" in text
    assert "/var/lib/minihci" in text
    assert "HOST_MEM_MB:-2560" in text
    assert "--ssh-inject" in text
    assert '"$disk.tmp"' in text
    assert 'sudo mv "$disk.tmp" "$disk"' in text
    assert "realpath" in text
