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


def test_dr_mount_declared_same_path_and_hint():
    text = SCRIPT.read_text()
    assert 'HOST_DR_MOUNT="${HOST_DR_MOUNT:-/srv/minihci/dr}"' in text
    assert "host_dr_mount" in text
    assert "$NFS_DIR/dr" in text
    assert "minihci hosts add laptop qemu:///system --site dr" in text


def _run_dr_mount(tmp_path, fstab_text, times):
    """Run only host_dr_mount against a local fstab; rssh is faked to run the command locally."""
    import re

    m = re.search(r"^host_dr_mount\(\) \{.*?^\}", SCRIPT.read_text(), re.DOTALL | re.MULTILINE)
    assert m, "host_dr_mount() not found"
    fstab = tmp_path / "fstab"
    fstab.write_text(fstab_text)
    prog = (
        "set -e\n"
        "LAPTOP_IP=192.168.122.1; NFS_DIR=/srv/minihci; HOST_DR_MOUNT=/srv/minihci/dr\n"
        f"FSTAB='{fstab}'\n"
        'rssh() { local n="$1"; shift; local c="$*"; c="${c//\\/etc\\/fstab/$FSTAB}";'
        ' c="${c//mkdir -p/true}"; c="${c//mountpoint -q/true}"; bash -c "$c"; }\n'
        f"{m.group(0)}\n" + "host_dr_mount 1\n" * times
    )
    r = subprocess.run(["bash", "-c", prog], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    return fstab.read_text()


def test_dr_fstab_line_once_after_two_runs(tmp_path):
    out = _run_dr_mount(tmp_path, "/dev/vda1 / xfs defaults 0 0\n", 2)
    lines = [ln for ln in out.splitlines() if "/srv/minihci/dr" in ln]
    assert lines == ["192.168.122.1:/srv/minihci/dr /srv/minihci/dr nfs4 vers=4.2,hard,_netdev 0 0"]


def test_dr_mount_keeps_existing_hand_made_line(tmp_path):
    mine = "192.168.122.1:/srv/minihci/dr /srv/minihci/dr nfs4 defaults 0 0\n"
    assert _run_dr_mount(tmp_path, mine, 1) == mine
