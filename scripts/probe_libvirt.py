#!/usr/bin/env python3
"""Probe the real hosts with libvirt-python before the libvirt driver is written.

Answers the questions the overnight queue (Q10, Q12) depends on:
  1. Does libvirt-python import in this environment?
  2. Can this user open qemu+ssh connections to the hosts (no sudo)?
  3. Can a qcow2 overlay be created through a libvirt dir pool on the NFS share?
  4. Does managed migrate3 (LIVE|PERSIST_DEST|UNDEFINE_SOURCE) work, and where do
     completed job stats (downtime, time_elapsed) come from?
  5. How long does connecting to a powered-off host take to fail?

Creates one throwaway domain `mhci-probe` and its overlay, and removes both at the end.
Does not touch test1 or any other domain.

  uv run --with libvirt-python python scripts/probe_libvirt.py
"""
import argparse
import json
import re
import sys
import threading
import time

try:
    import libvirt
except ImportError as e:
    sys.exit(f"FINDING import_libvirt: FAIL ({e}). Install: sudo dnf install -y libvirt-devel python3-devel gcc pkgconf-pkg-config")

libvirt.registerErrorHandler(lambda ctx, err: None, None)  # keep libvirt's own stderr quiet
findings = {}


def start_event_loop():
    """Keepalive pings need a libvirt event loop, registered BEFORE any connection is opened
    and run in a background thread. The real driver must do the same, once per process."""
    libvirt.virEventRegisterDefaultImpl()

    def loop():
        while True:
            libvirt.virEventRunDefaultImpl()

    threading.Thread(target=loop, daemon=True, name="libvirt-events").start()


def finding(key, value):
    findings[key] = value
    print(f"FINDING {key}: {value}", flush=True)


def with_timeout(fn, seconds):
    box = {}

    def run():
        try:
            box["ok"] = fn()
        except Exception as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=run, daemon=True)
    start = time.monotonic()
    t.start()
    t.join(seconds)
    elapsed = round(time.monotonic() - start, 2)
    if t.is_alive():
        return "timeout", elapsed, None
    if "err" in box:
        return "error", elapsed, box["err"]
    return "ok", elapsed, box["ok"]


def ensure_pool(conn, name, path):
    try:
        pool = conn.storagePoolLookupByName(name)
        created = False
    except libvirt.libvirtError:
        xml = f"<pool type='dir'><name>{name}</name><target><path>{path}</path></target></pool>"
        pool = conn.storagePoolDefineXML(xml, 0)
        created = True
    if not pool.isActive():
        pool.create(0)
    pool.setAutostart(1)
    pool.refresh(0)
    return pool, created


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


def wait_running(dom, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if dom.state()[0] == libvirt.VIR_DOMAIN_RUNNING:
            return True
        time.sleep(0.5)
    return False


def job_stats(dom):
    try:
        return dom.jobStats(libvirt.VIR_DOMAIN_JOB_STATS_COMPLETED)
    except libvirt.libvirtError as e:
        return {"error": str(e)}


def cleanup(conns, name, vol_path):
    for h, c in conns.items():
        try:
            d = c.lookupByName(name)
            if d.isActive():
                d.destroy()
            d.undefine()
            print(f"cleanup: removed {name} on {h}")
        except libvirt.libvirtError:
            pass
    for h, c in conns.items():
        try:
            c.storageVolLookupByPath(vol_path).delete(0)
            print(f"cleanup: deleted {vol_path} (via {h})")
            break
        except libvirt.libvirtError:
            continue


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="host1")
    ap.add_argument("--dst", default="host2")
    ap.add_argument("--dead", default="host3", help="a host that is powered off")
    ap.add_argument("--uri", default="qemu+ssh://root@{h}/system")
    ap.add_argument("--base", default="/var/lib/minihci")
    ap.add_argument("--image", default="cirros.img")
    ap.add_argument("--rounds", type=int, default=3, help="migrations (alternating direction)")
    a = ap.parse_args()
    name = "mhci-probe"
    vol_path = f"{a.base}/vms/{name}.qcow2"
    image_path = f"{a.base}/images/{a.image}"

    finding("import_libvirt", f"ok (bindings {libvirt.getVersion()})")
    start_event_loop()
    finding("event_loop", "registered; default impl running in a background thread")

    conns = {}
    for h in (a.src, a.dst):
        status, secs, val = with_timeout(lambda h=h: libvirt.open(a.uri.format(h=h)), 30)
        if status != "ok":
            finding(f"connect_{h}", f"FAIL {status} after {secs}s: {val}")
            sys.exit("Cannot connect. Check: `ssh root@host1 true` works as THIS user (not sudo).")
        conns[h] = val
        try:
            val.setKeepAlive(5, 3)
            finding(f"keepalive_{h}", "ok (5 s interval, 3 misses)")
        except libvirt.libvirtError as e:
            finding(f"keepalive_{h}", f"FAIL {e}")
        info = val.getInfo()
        finding(f"connect_{h}", f"ok in {secs}s; libvirt {val.getLibVersion()}, "
                f"{info[2]} cpus, {info[1]} MB, hostname={val.getHostname()}")

    src, dst = conns[a.src], conns[a.dst]
    cpu1 = src.getCPUStats(libvirt.VIR_NODE_CPU_STATS_ALL_CPUS)
    mem = src.getMemoryStats(libvirt.VIR_NODE_MEMORY_STATS_ALL_CELLS)
    finding("host_stats_keys", f"cpu={sorted(cpu1)} mem={sorted(mem)}")

    try:
        # Storage pools and the overlay
        _, img_created = ensure_pool(src, "minihci-images", f"{a.base}/images")
        _, vms_created = ensure_pool(src, "minihci-vms", f"{a.base}/vms")
        ensure_pool(dst, "minihci-images", f"{a.base}/images")
        ensure_pool(dst, "minihci-vms", f"{a.base}/vms")
        finding("pools", f"ok (defined new: images={img_created}, vms={vms_created}; also ensured on {a.dst})")
        base_vol = src.storageVolLookupByPath(image_path)
        cap = base_vol.info()[1]
        m = re.search(r"<format type='(\w+)'", base_vol.XMLDesc(0))
        base_fmt = m.group(1) if m else "raw"
        finding("base_image", f"{image_path} format={base_fmt} capacity={cap} bytes")
        cleanup(conns, name, vol_path)  # leftovers from an earlier probe run
        vms_pool = src.storagePoolLookupByName("minihci-vms")
        vol_xml = f"""<volume><name>{name}.qcow2</name><capacity unit='bytes'>{cap}</capacity>
  <target><format type='qcow2'/></target>
  <backingStore><path>{image_path}</path><format type='{base_fmt}'/></backingStore></volume>"""
        vms_pool.createXML(vol_xml, 0)
        finding("overlay_create", f"ok {vol_path} (backing {base_fmt})")

        # Define and start
        dom = src.defineXML(domain_xml(name, vol_path))
        dom.create()
        finding("define_start", f"running={wait_running(dom)} on {a.src}")
        time.sleep(8)  # let the guest boot a little

        # Managed live migration, alternating direction
        flags = (libvirt.VIR_MIGRATE_LIVE | libvirt.VIR_MIGRATE_PERSIST_DEST
                 | libvirt.VIR_MIGRATE_UNDEFINE_SOURCE)
        here, there = a.src, a.dst
        results = []
        for i in range(a.rounds):
            cur = conns[here].lookupByName(name)
            t0 = time.monotonic()
            try:
                new = cur.migrate3(conns[there], {}, flags)
            except libvirt.libvirtError as e:
                finding(f"migrate_{i + 1}", f"FAIL {here}->{there}: {e}")
                break
            wall_ms = round((time.monotonic() - t0) * 1000)
            dst_stats = job_stats(new)
            src_stats = job_stats(cur)  # usually gone after UNDEFINE_SOURCE
            row = {"dir": f"{here}->{there}", "wall_ms": wall_ms,
                   "dst_downtime_ms": dst_stats.get("downtime"),
                   "dst_time_elapsed_ms": dst_stats.get("time_elapsed"),
                   "src_stats": "error" if "error" in src_stats else sorted(src_stats)[:6],
                   "dst_stat_keys": sorted(k for k in dst_stats if k != "error")[:12],
                   "dst_error": dst_stats.get("error")}
            results.append(row)
            finding(f"migrate_{i + 1}", json.dumps(row))
            persisted = there in conns and conns[there].lookupByName(name).isPersistent()
            gone = True
            try:
                conns[here].lookupByName(name)
                gone = False
            except libvirt.libvirtError:
                pass
            finding(f"migrate_{i + 1}_flags", f"persistent_on_dst={persisted} undefined_on_src={gone}")
            here, there = there, here
            time.sleep(3)

        # Overlay deletion goes through the volume
        cleanup(conns, name, vol_path)
        try:
            src.storageVolLookupByPath(vol_path)
            finding("overlay_delete", "FAIL volume still present")
        except libvirt.libvirtError:
            finding("overlay_delete", "ok")
    finally:
        cleanup(conns, name, vol_path)

    # Dead host: how long until open() gives up?
    status, secs, val = with_timeout(lambda: libvirt.open(a.uri.format(h=a.dead)), 60)
    finding("dead_host_connect", f"{status} after {secs}s"
            + (f": {str(val)[:120]}" if status == "error" else "")
            + ("  (still hanging at 60 s: the driver MUST wrap connects in a timeout)" if status == "timeout" else ""))

    print("\n===== SUMMARY (paste this back) =====")
    print(json.dumps(findings, indent=1))


if __name__ == "__main__":
    main()
