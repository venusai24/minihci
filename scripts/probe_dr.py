#!/usr/bin/env python3
"""Probe libvirt checkpoint/backup behaviour on the real lab before the DR code (M5 Task 4) is written.

Owner runs this; it refuses to run without MINIHCI_REAL_HOSTS=1. It prints one
`Q<n>: PASS|FAIL <detail>` line per question and a JSON summary at the end. PASS means the
behaviour the DR design assumes held; every line carries the facts (error text, keys, sizes)
Task 4 needs either way.

  Q1 full push backup into a PRE-CREATED blank qcow2 on the DR share (REUSE_EXTERNAL, bitmap
     checkpoint); completed jobStats keys, and whether KEEP_COMPLETED is needed to read them
  Q2 incremental into an overlay backed by the full copy; guest suspended; qemu-img compare
  Q3 allocation (vol.info()[2]) of the full copy and the incremental after an idle minute
  Q4 migrate3 live with a checkpoint present; undefine() with checkpoints; undefineFlags(...)
  Q5 a fresh domain on the same disk (stale bitmaps, no libvirt metadata): new full backup
  Q6 abortJob() during a backup: is the new checkpoint still listed?
  Q7 boot the incremental chain top through a fresh overlay on --dr-uri for 10 s

Creates only domains and files named mhci-dr-probe*; --disk must be named that way too, because
cleanup deletes it. Does not touch test1 or any other domain.

  MINIHCI_REAL_HOSTS=1 uv run python scripts/probe_dr.py \\
      --src qemu+ssh://root@host1/system --dr-uri qemu:///system \\
      --dr-root /srv/minihci/dr --disk /var/lib/minihci/vms/mhci-dr-probe.qcow2
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from xml.sax.saxutils import escape

try:
    from scripts import benchlib
except ImportError:  # run as `python scripts/probe_dr.py`
    import benchlib  # type: ignore[no-redef]

NAME = "mhci-dr-probe"
BOOT_NAME = "mhci-dr-probe-boot"
results: list[dict] = []
lv = None  # the libvirt module, imported lazily in main()


def record(q, ok, detail):
    results.append({"q": q, "ok": bool(ok), "detail": detail})
    print(benchlib.format_probe_line(q, bool(ok), detail), flush=True)


def start_event_loop():
    """Must run BEFORE any connection is opened (see probe_libvirt.py)."""
    lv.virEventRegisterDefaultImpl()

    def loop():
        while True:
            lv.virEventRunDefaultImpl()

    threading.Thread(target=loop, daemon=True, name="libvirt-events").start()


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


def checkpoint_xml(name):
    return (f"<domaincheckpoint><name>{name}</name><disks>"
            "<disk name='vda' checkpoint='bitmap'/></disks></domaincheckpoint>")


def backup_xml(target, incremental=None):
    inc = f"<incremental>{incremental}</incremental>" if incremental else ""
    return (f"<domainbackup mode='push'>{inc}<disks><disk name='vda' type='file'>"
            f"<target file='{escape(target)}'/><driver type='qcow2'/></disk></disks></domainbackup>")


class Pools:
    """Finds (or creates a transient) dir pool for a directory so volumes can be made through libvirt."""

    def __init__(self):
        self.made = []  # (conn, pool) transient pools we started

    def for_dir(self, conn, directory):
        for p in conn.listAllStoragePools(0):
            m = re.search(r"<path>([^<]+)</path>", p.XMLDesc(0))
            if m and m.group(1).rstrip("/") == directory.rstrip("/") and p.isActive():
                p.refresh(0)
                return p
        label = NAME + "-pool-" + re.sub(r"\W+", "-", directory).strip("-")
        pool = conn.storagePoolCreateXML(
            f"<pool type='dir'><name>{label}</name><target><path>{directory}</path></target></pool>", 0)
        self.made.append((conn, pool))
        return pool

    def create_vol(self, conn, path, capacity, backing=None):
        pool = self.for_dir(conn, os.path.dirname(path))
        back = (f"<backingStore><path>{backing}</path><format type='qcow2'/></backingStore>"
                if backing else "")
        pool.createXML(
            f"<volume><name>{os.path.basename(path)}</name><capacity unit='bytes'>{capacity}</capacity>"
            f"<target><format type='qcow2'/></target>{back}</volume>", 0)

    def release(self):
        for _, pool in self.made:
            try:
                pool.destroy()
            except lv.libvirtError:
                pass


def wait_job_done(dom, seconds=300):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if dom.jobInfo()[0] == lv.VIR_DOMAIN_JOB_NONE:
            return True
        time.sleep(0.5)
    return False


def stats_keys(dom, flags):
    try:
        return sorted(dom.jobStats(flags))
    except lv.libvirtError as e:
        return f"error: {e}"


def run_backup(dom, target, cp, incremental=None):
    dom.backupBegin(backup_xml(target, incremental), checkpoint_xml(cp),
                    lv.VIR_DOMAIN_BACKUP_BEGIN_REUSE_EXTERNAL)
    return wait_job_done(dom)


def sh(cmd):
    r = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=120)
    return r.returncode, (r.stdout + r.stderr).strip()


def vol_alloc(conn, path):
    return conn.storageVolLookupByPath(path).info()[2]


def checkpoint_names(dom):
    return sorted(c.getName() for c in dom.listAllCheckpoints(0))


def remove_domain(conn, name):
    try:
        d = conn.lookupByName(name)
    except lv.libvirtError:
        return
    try:
        if d.isActive():
            d.destroy()
        d.undefineFlags(lv.VIR_DOMAIN_UNDEFINE_CHECKPOINTS_METADATA | lv.VIR_DOMAIN_UNDEFINE_NVRAM)
    except lv.libvirtError:
        try:
            d.undefine()
        except lv.libvirtError:
            pass


def remove_vol(conn, path):
    try:
        conn.storageVolLookupByPath(path).delete(0)
        print(f"cleanup: deleted {path}")
    except lv.libvirtError:
        pass


def cleanup(src, dr, dst, paths):
    for c in (src, dr, dst):
        if c is None:
            continue
        for n in (NAME, BOOT_NAME):
            remove_domain(c, n)
    for p in paths:  # overlays first, backing files last (list is ordered top-down)
        for c in (dr, src):
            if c is not None:
                remove_vol(c, p)


def q1(src, dom, full, cap):
    dom_ok = run_backup(dom, full, "cp1")
    plain = stats_keys(dom, lv.VIR_DOMAIN_JOB_STATS_COMPLETED)
    plain_again = stats_keys(dom, lv.VIR_DOMAIN_JOB_STATS_COMPLETED)
    keep = stats_keys(dom, lv.VIR_DOMAIN_JOB_STATS_COMPLETED | lv.VIR_DOMAIN_JOB_STATS_KEEP_COMPLETED)
    keep_again = stats_keys(dom, lv.VIR_DOMAIN_JOB_STATS_COMPLETED)
    cps = checkpoint_names(dom)
    needed = not isinstance(plain, list) or not plain
    record("Q1", dom_ok and "cp1" in cps,
           json.dumps({"job_finished": dom_ok, "checkpoints": cps,
                       "completed_keys_plain_read": plain,
                       "plain_read_repeated": plain_again,
                       "keep_completed_read": keep, "after_keep_read": keep_again,
                       "keep_completed_needed_after_NONE": needed}))


def q2(dom, vm_disk, full, inc):
    dom.suspend()
    try:
        done = run_backup(dom, inc, "cp2", incremental="cp1")
        keys = stats_keys(dom, lv.VIR_DOMAIN_JOB_STATS_COMPLETED | lv.VIR_DOMAIN_JOB_STATS_KEEP_COMPLETED)
        rc, out = sh(["qemu-img", "compare", "-U", "-f", "qcow2", "-F", "qcow2", vm_disk, inc])
        same = benchlib.parse_qemu_img_compare(out)
    finally:
        dom.resume()
    record("Q2", done and same,
           json.dumps({"job_finished": done, "qemu_img_rc": rc, "qemu_img_out": out[:200],
                       "identical": same, "completed_keys": keys, "guest_resumed": True}))


def q3(dr, full, inc, idle):
    time.sleep(idle)
    a_full, a_inc = vol_alloc(dr, full), vol_alloc(dr, inc)
    record("Q3", True, json.dumps({"idle_s": idle, "full_alloc_bytes": a_full, "incr_alloc_bytes": a_inc}))


def q4(dom, src, dst, dst_uri):
    facts = {}
    ok = True
    if dst is None:
        facts["migrate3"] = "skipped (no --dst-uri)"
    else:
        flags = lv.VIR_MIGRATE_LIVE | lv.VIR_MIGRATE_PERSIST_DEST | lv.VIR_MIGRATE_UNDEFINE_SOURCE
        try:
            new = dom.migrate3(dst, {}, flags)
            facts["migrate3"] = "NOT refused (migrated)"
            facts["checkpoints_on_dst"] = checkpoint_names(new)
            back = new.migrate3(src, {}, flags)  # bring it home for the later questions
            facts["migrated_back"] = True
            dom = back
        except lv.libvirtError as e:
            facts["migrate3"] = f"refused: {e}"
    dom = src.lookupByName(NAME)
    dom.destroy()  # undefine() with checkpoints is only checked on an inactive domain
    try:
        dom.undefine()
        facts["undefine"] = "NOT refused"
        ok = False
    except lv.libvirtError as e:
        facts["undefine"] = f"refused: {e}"
    try:
        dom.undefineFlags(lv.VIR_DOMAIN_UNDEFINE_CHECKPOINTS_METADATA)
        facts["undefineFlags_CHECKPOINTS_METADATA"] = "ok"
    except lv.libvirtError as e:
        facts["undefineFlags_CHECKPOINTS_METADATA"] = f"FAIL: {e}"
        ok = False
    record("Q4", ok, json.dumps(facts))


def q5(src, vm_disk, blank, cap, pools):
    pools.create_vol(src, blank, cap)
    dom = src.defineXML(domain_xml(NAME, vm_disk))
    dom.create()
    time.sleep(5)
    pre = checkpoint_names(dom)
    try:
        done = run_backup(dom, blank, "cp-fresh")
        record("Q5", done and "cp-fresh" in checkpoint_names(dom),
               json.dumps({"checkpoints_before": pre, "job_finished": done,
                           "checkpoints_after": checkpoint_names(dom)}))
    except lv.libvirtError as e:
        record("Q5", False, f"new full backup refused with stale bitmaps: {e}")
    return dom


def q6(dom, blank2, cap, pools, dr):
    pools.create_vol(dr, blank2, cap)
    before = checkpoint_names(dom)
    try:
        dom.backupBegin(backup_xml(blank2), checkpoint_xml("cp-abort"),
                        lv.VIR_DOMAIN_BACKUP_BEGIN_REUSE_EXTERNAL)
        dom.abortJob()
        time.sleep(2)
        after = checkpoint_names(dom)
        record("Q6", "cp-abort" not in after,
               json.dumps({"checkpoints_before": before, "checkpoints_after_abort": after,
                           "new_checkpoint_still_listed": "cp-abort" in after,
                           "note": "PASS = the aborted checkpoint is gone"}))
    except lv.libvirtError as e:
        record("Q6", False, f"backupBegin/abortJob error: {e}")


def q7(dr, inc, boot_disk, cap, pools):
    pools.create_vol(dr, boot_disk, cap, backing=inc)
    dom = dr.defineXML(domain_xml(BOOT_NAME, boot_disk))
    dom.create()
    end = time.monotonic() + 10
    steady = True
    while time.monotonic() < end:
        if dom.state()[0] != lv.VIR_DOMAIN_RUNNING:
            steady = False
            break
        time.sleep(0.5)
    record("Q7", steady, json.dumps({"state_after_10s": dom.state()[0], "running_10s": steady}))


def guarded(label, fn, *args):
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        record(label, False, f"exception: {type(e).__name__}: {e}")
        return None


def main():
    global lv
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="qemu+ssh://root@host1/system", help="host that runs the probe guest")
    ap.add_argument("--dr-uri", default="qemu:///system", help="host that mounts only the DR share")
    ap.add_argument("--dr-root", default="/srv/minihci/dr")
    ap.add_argument("--disk", default="/var/lib/minihci/vms/mhci-dr-probe.qcow2",
                    help="probe guest disk to create (an overlay of --image); deleted at the end")
    ap.add_argument("--image", default="/var/lib/minihci/images/cirros.img", help="qcow2 base image")
    ap.add_argument("--dst-uri", default="qemu+ssh://root@host2/system",
                    help="migrate3 target for Q4 (must mount the primary share); '' skips")
    ap.add_argument("--idle", type=int, default=60, help="seconds to idle before Q3")
    a = ap.parse_args()

    benchlib.require_real_hosts()
    if not os.path.basename(a.disk).startswith(NAME):
        sys.exit(f"--disk must be named {NAME}*: cleanup deletes it.")

    try:
        import libvirt
    except ImportError as e:
        sys.exit(f"libvirt-python missing ({e}); install the extra: uv sync --extra libvirt")
    lv = libvirt
    lv.registerErrorHandler(lambda ctx, err: None, None)
    start_event_loop()

    full = f"{a.dr_root}/{NAME}-full.qcow2"
    inc = f"{a.dr_root}/{NAME}-inc1.qcow2"
    blank = f"{a.dr_root}/{NAME}-blank-q5.qcow2"
    blank2 = f"{a.dr_root}/{NAME}-blank-q6.qcow2"
    boot = f"{a.dr_root}/{NAME}-boot.qcow2"
    top_down = [boot, blank2, blank, inc, full, a.disk]

    src = dr = dst = None
    pools = Pools()
    try:
        src = lv.open(a.src)
        dr = lv.open(a.dr_uri)
        dst = lv.open(a.dst_uri) if a.dst_uri else None
        for c in (src, dr, dst):
            if c is not None:
                c.setKeepAlive(5, 3)
        cleanup(src, dr, dst, top_down)  # leftovers from an earlier run
        cap = src.storageVolLookupByPath(a.image).info()[1]
        pools.create_vol(src, a.disk, cap, backing=a.image)
        dom = src.defineXML(domain_xml(NAME, a.disk))
        dom.create()
        time.sleep(8)  # let the guest boot a little
        pools.create_vol(dr, full, cap)
        pools.create_vol(dr, inc, cap, backing=full)  # overlay on the full copy

        guarded("Q1", q1, src, dom, full, cap)
        guarded("Q2", q2, dom, a.disk, full, inc)
        guarded("Q3", q3, dr, full, inc, a.idle)
        guarded("Q4", q4, dom, src, dst, a.dst_uri)
        dom5 = guarded("Q5", q5, src, a.disk, blank, cap, pools)
        if dom5 is not None:
            guarded("Q6", q6, dom5, blank2, cap, pools, dr)
        guarded("Q7", q7, dr, inc, boot, cap, pools)
    except Exception as e:  # noqa: BLE001
        record("SETUP", False, f"{type(e).__name__}: {e}")
    finally:
        cleanup(src, dr, dst, top_down)
        pools.release()

    print("\n===== SUMMARY (paste this back) =====")
    print(json.dumps({"summary": benchlib.summarise_probe(results), "results": results}, indent=1))


if __name__ == "__main__":
    main()
