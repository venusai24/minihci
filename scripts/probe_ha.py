#!/usr/bin/env python3
"""HA failover dry run on real hosts, before the HA code (Q12/Q13) is written.

Measures what the overnight queue can't:
  1. How fast libvirt notices a host power cut (keepalive close callback, fresh connect).
  2. Whether the VM's disk can be started on another host right away, or whether
     QEMU's image lock over NFS blocks it until the lock lease expires — and for how long.
  3. What the dead host still has defined when it comes back (the stale copy to fence).

Flow: create `mhci-ha-probe` on --victim (host2), `sudo virsh destroy <victim>` on this laptop
(a power cut), restart the VM on --survivor (host1) retrying every 2 s, then power the victim
back on and clean up. Never touches test1 or any other domain.

  sudo -v && uv run --with libvirt-python python scripts/probe_ha.py
"""
import argparse
import json
import re
import subprocess
import sys
import threading
import time

try:
    import libvirt
except ImportError as e:
    sys.exit(f"libvirt-python missing ({e}); run with: uv run --with libvirt-python ...")

libvirt.registerErrorHandler(lambda ctx, err: None, None)
findings = {}
T0 = None  # moment of the power cut


def finding(key, value):
    findings[key] = value
    print(f"FINDING {key}: {value}", flush=True)


def since_cut():
    return round(time.monotonic() - T0, 2)


def start_event_loop():
    libvirt.virEventRegisterDefaultImpl()

    def loop():
        while True:
            libvirt.virEventRunDefaultImpl()

    threading.Thread(target=loop, daemon=True, name="libvirt-events").start()


def open_with_timeout(uri, seconds):
    box = {}

    def run():
        try:
            box["ok"] = libvirt.open(uri)
        except Exception as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        return None, "timeout"
    return box.get("ok"), box.get("err")


def sh(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


def ensure_pool(conn, name, path):
    try:
        pool = conn.storagePoolLookupByName(name)
    except libvirt.libvirtError:
        pool = conn.storagePoolDefineXML(
            f"<pool type='dir'><name>{name}</name><target><path>{path}</path></target></pool>", 0)
    if not pool.isActive():
        pool.create(0)
    pool.setAutostart(1)
    pool.refresh(0)
    return pool


def domain_xml(name, disk):
    return f"""<domain type='kvm'>
  <name>{name}</name>
  <memory unit='MiB'>256</memory>
  <vcpu>1</vcpu>
  <os><type arch='x86_64' machine='q35'>hvm</type><boot dev='hd'/></os>
  <features><acpi/><apic/></features>
  <cpu mode='host-passthrough'/>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' cache='none'/>
      <source file='{disk}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <serial type='pty'/>
    <console type='pty'/>
  </devices>
</domain>"""


def remove_domain(conn, name):
    try:
        d = conn.lookupByName(name)
        if d.isActive():
            d.destroy()
        d.undefine()
        return True
    except libvirt.libvirtError:
        return False


def main():
    global T0
    ap = argparse.ArgumentParser()
    ap.add_argument("--survivor", default="host1")
    ap.add_argument("--victim", default="host2")
    ap.add_argument("--uri", default="qemu+ssh://root@{h}/system")
    ap.add_argument("--base", default="/var/lib/minihci")
    ap.add_argument("--image", default="cirros.img")
    ap.add_argument("--max-wait", type=int, default=240, help="seconds to keep retrying the restart")
    a = ap.parse_args()
    name = "mhci-ha-probe"
    disk = f"{a.base}/vms/{name}.qcow2"
    image = f"{a.base}/images/{a.image}"

    rc, _ = sh(["sudo", "-n", "true"])
    if rc != 0:
        sys.exit("Run `sudo -v` first (the script powers the victim off with sudo virsh).")

    # NFS facts
    for f in ("nfsv4leasetime", "nfsv4gracetime"):
        rc, out = sh(["sudo", "-n", "cat", f"/proc/fs/nfsd/{f}"])
        finding(f"nfsd_{f}", out if rc == 0 else f"unreadable ({out})")
    rc, out = sh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", f"root@{a.survivor}",
                  "findmnt", "-no", "FSTYPE,OPTIONS", a.base])
    finding("nfs_mount_on_survivor", out)

    start_event_loop()
    conns = {}
    for h in (a.survivor, a.victim):
        c, err = open_with_timeout(a.uri.format(h=h), 20)
        if not c:
            sys.exit(f"cannot connect to {h}: {err}. Start it: sudo virsh start {h}")
        c.setKeepAlive(2, 2)  # notice a dead host in about 4-6 s
        conns[h] = c
    surv, vic = conns[a.survivor], conns[a.victim]

    closed = {}

    def on_close(conn, reason, opaque):
        closed["t"] = since_cut() if T0 else None
        closed["reason"] = reason

    vic.registerCloseCallback(on_close, None)

    for c in conns.values():
        ensure_pool(c, "minihci-images", f"{a.base}/images")
        ensure_pool(c, "minihci-vms", f"{a.base}/vms")
    for c in conns.values():
        remove_domain(c, name)
    try:
        surv.storagePoolLookupByName("minihci-vms").refresh(0)
        surv.storageVolLookupByPath(disk).delete(0)
    except libvirt.libvirtError:
        pass

    base = vic.storageVolLookupByPath(image)
    cap = base.info()[1]
    fmt = (re.search(r"<format type='(\w+)'", base.XMLDesc(0)) or [None, "qcow2"])[1]
    vic.storagePoolLookupByName("minihci-vms").createXML(
        f"<volume><name>{name}.qcow2</name><capacity unit='bytes'>{cap}</capacity>"
        f"<target><format type='qcow2'/></target>"
        f"<backingStore><path>{image}</path><format type='{fmt}'/></backingStore></volume>", 0)
    dom = vic.defineXML(domain_xml(name, disk))
    dom.create()
    time.sleep(10)  # guest boots and QEMU holds its image lock
    finding("vm_on_victim", f"running={dom.state()[0] == libvirt.VIR_DOMAIN_RUNNING}")

    # Power cut
    print(f"\n*** powering off {a.victim} (sudo virsh destroy) ***\n", flush=True)
    T0 = time.monotonic()
    rc, out = sh(["sudo", "-n", "virsh", "destroy", a.victim])
    finding("power_cut", f"rc={rc} {out}")

    # Fresh-connect failure time (what a heartbeat ping would see), in the background so the
    # restart below starts at once and measures the disk-lock wait on its own.
    fresh = {}

    def fresh_check():
        c, err = open_with_timeout(a.uri.format(h=a.victim), 60)
        fresh["v"] = f"{'CONNECTED?!' if c else 'failed'} at t={since_cut()}s: {str(err)[:100]}"

    fc = threading.Thread(target=fresh_check, daemon=True)
    fc.start()

    # Restart on the survivor, retrying every 2 s
    surv_dom = surv.defineXML(domain_xml(name, disk))
    attempts, errors = 0, {}
    started_at = None
    while since_cut() < a.max_wait:
        attempts += 1
        try:
            surv_dom.create()
            started_at = since_cut()
            break
        except libvirt.libvirtError as e:
            msg = str(e)
            key = "write lock" if "lock" in msg.lower() else msg[:80]
            errors.setdefault(key, {"first_t": since_cut(), "count": 0, "example": msg[:200]})
            errors[key]["count"] += 1
            errors[key]["last_t"] = since_cut()
            time.sleep(2)
    finding("create_call", "blocked until it succeeded" if attempts == 1 and started_at and started_at > 5
            else "returned quickly")
    for _ in range(int(a.max_wait)):
        if not fc.is_alive():
            break
        time.sleep(1)
    finding("fresh_connect_after_cut", fresh.get("v", "still hanging"))
    finding("keepalive_close_callback", f"t={closed.get('t')}s reason={closed.get('reason')}"
            if closed else "never fired")
    finding("restart_errors", json.dumps(errors))
    finding("restart_on_survivor", f"running at t={started_at}s after {attempts} attempts"
            if started_at is not None else f"FAILED after {a.max_wait}s and {attempts} attempts")

    # Bring the victim back and inspect the stale copy
    print(f"\n*** powering {a.victim} back on ***\n", flush=True)
    sh(["sudo", "-n", "virsh", "start", a.victim])
    back = None
    for _ in range(40):
        back, _ = open_with_timeout(a.uri.format(h=a.victim), 5)
        if back:
            break
        time.sleep(3)
    if back:
        try:
            stale = back.lookupByName(name)
            state = {1: "running", 5: "shut off"}.get(stale.state()[0], str(stale.state()[0]))
            finding("stale_copy_on_victim", f"defined, state={state}, autostart={stale.autostart()}")
        except libvirt.libvirtError:
            finding("stale_copy_on_victim", "not defined")
        remove_domain(back, name)
        finding("victim_back", "reconnected; stale copy removed")
    else:
        finding("victim_back", "did not come back within 2 min; remove the stale copy by hand: "
                f"ssh root@{a.victim} virsh undefine {name}")

    # Cleanup on the survivor (refresh first: the volume was created through the victim's pool)
    remove_domain(surv, name)
    try:
        surv.storagePoolLookupByName("minihci-vms").refresh(0)
        surv.storageVolLookupByPath(disk).delete(0)
        finding("cleanup", "ok")
    except libvirt.libvirtError as e:
        finding("cleanup", f"volume delete failed: {e}")

    print("\n===== SUMMARY (paste this back) =====")
    print(json.dumps(findings, indent=1))


if __name__ == "__main__":
    main()
