"""In-memory stand-in for the `libvirt` python module (no real connection ever).

Use the `stub_libvirt` fixture (tests/conftest.py); it injects an instance into
sys.modules['libvirt'] and yields it. Test controls live on the module object:
  lib.host(uri)              -> FakeHost (domains, pools, cpu/mem counters, down flag,
                                hangs/fail dicts keyed by op name)
  lib.hang(uri, op="open")   -> threading.Event; the op blocks until .set()
  lib.event_loop_registered  -> set by virEventRegisterDefaultImpl()
  dom.backup_outcome / dom.backup_polls -> "completed" | "failed" | "hang"; jobInfo() polls before it ends
  host.backups / host.undefine_flags -> recorded backupBegin / undefineFlags calls
  conn.simulate_close(reason) -> marks the conn dead and fires close callbacks
  lib.open_calls             -> list of uris opened
Pools cache their volume list: only pool.refresh(0) syncs it from host.storage
(tests that model a shared NFS share assign the same dict to several hosts).
"""

from __future__ import annotations

import sys
import threading
import types
import xml.etree.ElementTree as ET

import pytest

ERR = {
    "VIR_ERR_OK": 0,
    "VIR_ERR_INTERNAL_ERROR": 1,
    "VIR_ERR_NO_CONNECT": 5,
    "VIR_ERR_INVALID_CONN": 6,
    "VIR_ERR_SYSTEM_ERROR": 38,
    "VIR_ERR_RPC": 39,
    "VIR_ERR_NO_DOMAIN": 42,
    "VIR_ERR_NO_STORAGE_POOL": 49,
    "VIR_ERR_NO_STORAGE_VOL": 50,
    "VIR_ERR_OPERATION_FAILED": 68,
    "VIR_ERR_OPERATION_INVALID": 55,
    "VIR_ERR_DOM_EXIST": 76,
    "VIR_ERR_NO_DOMAIN_CHECKPOINT": 103,
}
CONSTS = {
    "VIR_DOMAIN_NOSTATE": 0,
    "VIR_DOMAIN_RUNNING": 1,
    "VIR_DOMAIN_BLOCKED": 2,
    "VIR_DOMAIN_PAUSED": 3,
    "VIR_DOMAIN_SHUTDOWN": 4,
    "VIR_DOMAIN_SHUTOFF": 5,
    "VIR_DOMAIN_CRASHED": 6,
    "VIR_DOMAIN_PMSUSPENDED": 7,
    "VIR_MIGRATE_LIVE": 1,
    "VIR_MIGRATE_PEER2PEER": 2,
    "VIR_MIGRATE_PERSIST_DEST": 8,
    "VIR_MIGRATE_UNDEFINE_SOURCE": 16,
    "VIR_DOMAIN_JOB_STATS_COMPLETED": 1,
    "VIR_DOMAIN_JOB_STATS_KEEP_COMPLETED": 4,
    "VIR_DOMAIN_JOB_NONE": 0,
    "VIR_DOMAIN_JOB_BOUNDED": 1,
    "VIR_DOMAIN_JOB_UNBOUNDED": 2,
    "VIR_DOMAIN_JOB_COMPLETED": 3,
    "VIR_DOMAIN_JOB_FAILED": 4,
    "VIR_DOMAIN_JOB_CANCELLED": 5,
    "VIR_DOMAIN_JOB_OPERATION_BACKUP": 9,
    "VIR_DOMAIN_BACKUP_BEGIN_REUSE_EXTERNAL": 1,
    "VIR_DOMAIN_UNDEFINE_CHECKPOINTS_METADATA": 16,
    "VIR_NODE_CPU_STATS_ALL_CPUS": -1,
    "VIR_NODE_MEMORY_STATS_ALL_CELLS": -1,
    "VIR_CONNECT_CLOSE_REASON_ERROR": 0,
    "VIR_CONNECT_CLOSE_REASON_EOF": 1,
    "VIR_CONNECT_CLOSE_REASON_KEEPALIVE": 2,
    "VIR_CONNECT_CLOSE_REASON_CLIENT": 3,
}
RUNNING = CONSTS["VIR_DOMAIN_RUNNING"]
SHUTOFF = CONSTS["VIR_DOMAIN_SHUTOFF"]


class libvirtError(Exception):
    def __init__(self, msg="libvirt error", code=ERR["VIR_ERR_INTERNAL_ERROR"]):
        super().__init__(msg)
        self.code = code

    def get_error_code(self):
        return self.code

    def get_error_message(self):
        return str(self)


def _err(name, msg):
    return libvirtError(msg, ERR[name])


class FakeVolume:
    def __init__(self, pool, name, path, capacity, backing=None, backing_format="qcow2"):
        self.pool, self._name, self.path = pool, name, path
        self.capacity, self.backing, self.backing_format = capacity, backing, backing_format
        self.allocation = 0

    def name(self):
        return self._name

    def info(self):  # [type, capacity, allocation]
        return [0, self.capacity, self.allocation]

    def XMLDesc(self, flags=0):
        bs = ""
        if self.backing:
            bs = f"<backingStore><path>{self.backing}</path><format type='{self.backing_format}'/></backingStore>"
        return (
            f"<volume><name>{self._name}</name><key>{self.path}</key>"
            f"<capacity unit='bytes'>{self.capacity}</capacity>"
            f"<target><path>{self.path}</path><format type='qcow2'/></target>{bs}</volume>"
        )

    def delete(self, flags=0):
        self.pool.host.hook("vol_delete")
        self.pool.host.storage.pop(self.path, None)
        self.pool.visible.pop(self.path, None)


class FakePool:
    """`visible` is the pool's cached volume list; only refresh(0) syncs it from `host.storage`."""

    def __init__(self, host, name, path):
        self.host, self._name, self.path = host, name, path
        self.active = False
        self.autostart_ = False
        self.visible: dict[str, FakeVolume] = {}
        self.refresh_calls = 0

    def name(self):
        return self._name

    def isActive(self):
        return int(self.active)

    def XMLDesc(self, flags=0):
        return f"<pool type='dir'><name>{self._name}</name><target><path>{self.path}</path></target></pool>"

    def create(self, flags=0):
        self.active = True

    def setAutostart(self, v):
        self.autostart_ = bool(v)

    def refresh(self, flags=0):
        self.host.hook("pool_refresh")
        self.refresh_calls += 1
        prefix = self.path.rstrip("/") + "/"
        self.visible = {p: v for p, v in self.host.storage.items() if p.startswith(prefix)}

    def createXML(self, xml, flags=0):
        self.host.hook("vol_create")
        root = ET.fromstring(xml)
        name = root.findtext("name")
        cap = int(root.findtext("capacity"))
        path = f"{self.path.rstrip('/')}/{name}"
        if path in self.host.storage:
            raise _err("VIR_ERR_OPERATION_FAILED", f"volume {name} exists")
        bs = root.find("backingStore")
        backing = bs.findtext("path") if bs is not None else None
        fmt = bs.find("format") if bs is not None else None
        vol = FakeVolume(self, name, path, cap, backing, fmt.get("type") if fmt is not None else "qcow2")
        self.host.storage[path] = vol
        self.visible[path] = vol
        return vol

    def storageVolLookupByName(self, name):
        for v in self.visible.values():
            if v.name() == name:
                return v
        raise _err("VIR_ERR_NO_STORAGE_VOL", f"no volume {name}")


class FakeCheckpoint:
    def __init__(self, dom, name, ctime):
        self.dom, self._name, self.ctime = dom, name, ctime

    def getName(self):  # the real libvirt-python binding has getName(), not name()
        return self._name

    def getXMLDesc(self, flags=0):
        return (
            f"<domaincheckpoint><name>{self._name}</name><creationTime>{self.ctime}</creationTime></domaincheckpoint>"
        )

    def delete(self, flags=0):
        self.dom._check("checkpointDelete")
        if self.dom.checkpoint_delete_failures > 0:
            self.dom.checkpoint_delete_failures -= 1
            raise _err("VIR_ERR_OPERATION_FAILED", "checkpoint is busy")
        self.dom.checkpoints.pop(self._name, None)


class FakeDomain:
    def __init__(self, host, xml):
        self.host = host
        self._name = ET.fromstring(xml).findtext("name")
        self.xml = xml
        self.state_ = SHUTOFF
        self.reason_ = 1  # the second item of virDomainGetState
        self.persistent = True
        self.defined = True
        self.job_stats: dict[str, int] | Exception = {"downtime": 90, "time_elapsed": 400}
        self.checkpoints: dict[str, FakeCheckpoint] = {}
        self.backup_outcome = "completed"  # or "failed" / "hang" (never finishes)
        self.backup_polls = 1  # jobInfo() calls that still report the job before it ends
        self.backup_job: dict | None = None
        self.jobinfo_after_abort = 0  # jobInfo() calls made while an abort was pending
        self.abort_polls = 0  # jobInfo() polls that still report the job after abortJob()
        self.checkpoint_delete_failures = 0  # the next N checkpoint deletes fail
        self.completed_job: dict | None = None
        self._ctime = 0

    def _check(self, op):
        self.host.hook(op)
        if not self.defined:
            raise _err("VIR_ERR_NO_DOMAIN", f"domain {self._name} not found")

    def name(self):
        return self._name

    def XMLDesc(self, flags=0):
        self._check("xml")
        return self.xml

    def state(self, flags=0):
        self._check("state")
        return [self.state_, self.reason_]

    def isActive(self):
        self._check("isActive")
        return int(self.state_ not in (SHUTOFF, 0))

    def create(self):
        self._check("create")  # real libvirt may block here until the NFS lease expires
        if self.state_ == RUNNING:
            raise _err("VIR_ERR_OPERATION_INVALID", "domain is already running")
        self.state_ = RUNNING

    def _stop(self, op):
        self._check(op)
        if self.state_ != RUNNING:
            raise _err("VIR_ERR_OPERATION_INVALID", "domain is not running")
        self.state_ = SHUTOFF

    def destroy(self):
        self._check("destroy")
        if self.state_ in (SHUTOFF, 0):  # a paused or crashed domain is still active and can be destroyed
            raise _err("VIR_ERR_OPERATION_INVALID", "domain is not running")
        self.state_ = SHUTOFF

    def shutdown(self):
        self._stop("shutdown")

    def resume(self):
        self._check("resume")
        if self.state_ != CONSTS["VIR_DOMAIN_PAUSED"]:
            raise _err("VIR_ERR_OPERATION_INVALID", "domain is not paused")
        self.state_, self.reason_ = RUNNING, 1

    def reboot(self, flags=0):
        self._check("reboot")
        if self.state_ != RUNNING:
            raise _err("VIR_ERR_OPERATION_INVALID", "domain is not running")

    def undefine(self):
        self._check("undefine")
        self.host.domains.pop(self._name, None)
        self.defined = False

    def undefineFlags(self, flags=0):
        self.host.undefine_flags.append(flags)
        self.undefine()
        self.checkpoints.clear()

    def listAllCheckpoints(self, flags=0):
        self._check("listAllCheckpoints")
        return list(reversed(list(self.checkpoints.values())))  # deliberately not creation order

    def checkpointLookupByName(self, name, flags=0):
        self._check("checkpointLookup")
        cp = self.checkpoints.get(name)
        if cp is None:
            raise _err("VIR_ERR_NO_DOMAIN_CHECKPOINT", f"no domain checkpoint with matching name '{name}'")
        return cp

    def backupBegin(self, backup_xml, checkpoint_xml, flags=0):
        self._check("backupBegin")
        if self.state_ != RUNNING:
            raise _err("VIR_ERR_OPERATION_INVALID", "domain is not running")
        if self.backup_job is not None:
            raise _err("VIR_ERR_OPERATION_INVALID", "another job is active")
        broot = ET.fromstring(backup_xml)
        since = broot.findtext("incremental")
        if since is not None and since not in self.checkpoints:
            raise _err("VIR_ERR_NO_DOMAIN_CHECKPOINT", f"no domain checkpoint with matching name '{since}'")
        cname = ET.fromstring(checkpoint_xml).findtext("name")
        if cname in self.checkpoints:
            raise _err("VIR_ERR_OPERATION_INVALID", f"checkpoint {cname} already exists")
        target = broot.find("disks/disk/target").get("file")
        self.host.backups.append({"target": target, "since": since, "checkpoint": cname, "flags": flags})
        self._ctime += 1
        self.checkpoints[cname] = FakeCheckpoint(self, cname, self._ctime)
        self.backup_job = {"polls": self.backup_polls}
        self.completed_job = None

    def jobInfo(self):
        self._check("jobInfo")
        job = self.backup_job
        if job is not None and job.get("aborting") is not None:
            self.jobinfo_after_abort += 1
            if job["aborting"] > 0:
                job["aborting"] -= 1
                return [CONSTS["VIR_DOMAIN_JOB_UNBOUNDED"]] + [0] * 11
            self.backup_job = None
            return [CONSTS["VIR_DOMAIN_JOB_NONE"]] + [0] * 11
        if job is None:
            return [CONSTS["VIR_DOMAIN_JOB_NONE"], 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        if self.backup_outcome != "hang":
            if job["polls"] > 0:
                job["polls"] -= 1
            else:
                ok = self.backup_outcome == "completed"
                self.backup_job = None
                kind = "VIR_DOMAIN_JOB_COMPLETED" if ok else "VIR_DOMAIN_JOB_FAILED"
                self.completed_job = {"type": CONSTS[kind], "operation": CONSTS["VIR_DOMAIN_JOB_OPERATION_BACKUP"]}
                return [CONSTS["VIR_DOMAIN_JOB_NONE"], 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        return [CONSTS["VIR_DOMAIN_JOB_UNBOUNDED"], 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]

    def abortJob(self):
        self._check("abortJob")
        if self.backup_job is None:
            raise _err("VIR_ERR_OPERATION_INVALID", "no job is active on the domain")
        if self.abort_polls > 0:
            self.backup_job["aborting"] = self.abort_polls
        else:
            self.backup_job = None
        self.completed_job = {
            "type": CONSTS["VIR_DOMAIN_JOB_CANCELLED"],
            "operation": CONSTS["VIR_DOMAIN_JOB_OPERATION_BACKUP"],
        }

    def migrate3(self, dst_conn, params, flags):
        self._check("migrate")
        dst_conn._check()
        if self.state_ != RUNNING:
            raise _err("VIR_ERR_OPERATION_INVALID", "domain is not running")
        if flags & CONSTS["VIR_MIGRATE_PEER2PEER"]:
            raise _err("VIR_ERR_OPERATION_INVALID", "stub: PEER2PEER not supported (managed migration only)")
        dst = dst_conn.host
        if self._name in dst.domains:
            raise _err("VIR_ERR_DOM_EXIST", "domain exists on destination")
        self.host.migrations.append((dst.uri, self._name, dict(params), flags))
        new = FakeDomain(dst, self.xml)
        new.state_ = RUNNING
        new.job_stats = self.job_stats
        new.persistent = bool(flags & CONSTS["VIR_MIGRATE_PERSIST_DEST"])
        dst.domains[self._name] = new
        self.state_ = SHUTOFF
        if flags & CONSTS["VIR_MIGRATE_UNDEFINE_SOURCE"]:
            self.host.domains.pop(self._name, None)
            self.defined = False
        return new

    def jobStats(self, flags=0):
        # Like real libvirt: raises on the (undefined) source domain, works on the returned destination one.
        self._check("jobStats")
        if self.backup_job is not None:
            return {"type": CONSTS["VIR_DOMAIN_JOB_UNBOUNDED"], "operation": CONSTS["VIR_DOMAIN_JOB_OPERATION_BACKUP"]}
        if self.completed_job is not None and flags & CONSTS["VIR_DOMAIN_JOB_STATS_COMPLETED"]:
            out = dict(self.completed_job)
            if not flags & CONSTS["VIR_DOMAIN_JOB_STATS_KEEP_COMPLETED"]:
                self.completed_job = None
            return out
        if isinstance(self.job_stats, Exception):
            raise self.job_stats
        return dict(self.job_stats)


class FakeHost:
    def __init__(self, uri):
        self.uri = uri
        self.down = False
        self.domains: dict[str, FakeDomain] = {}
        self.pools: dict[str, FakePool] = {}
        self.storage: dict[str, FakeVolume] = {}  # truth on the (shared) filesystem
        self.cpu = {"user": 100, "kernel": 50, "idle": 800, "iowait": 10, "guest": 5}
        self.mem = {"total": 2_000_000, "free": 500_000, "buffers": 100_000, "cached": 400_000}  # KiB
        self.info = ["x86_64", 2456, 2, 2400, 1, 1, 2, 1]
        self.migrations: list[tuple] = []
        self.backups: list[dict] = []
        self.undefine_flags: list[int] = []
        self.hangs: dict[str, threading.Event] = {}
        self.fail: dict[str, Exception] = {}  # op -> exception raised on every call while set
        self.conns: list[FakeConn] = []

    def hook(self, op):
        ev = self.hangs.get(op)
        if ev is not None:
            ev.wait()
        exc = self.fail.get(op)
        if exc is not None:
            raise exc


class FakeConn:
    def __init__(self, lib, host):
        self.lib, self.host = lib, host
        self.alive = True
        self.keepalive = None
        self.close_cbs: list[tuple] = []
        self.closed = False
        self.close_count = 0
        self.closed_with_cb = False
        self.in_close_cb = False

    def _check(self, op=None):
        if not self.alive or self.host.down:
            raise _err("VIR_ERR_INVALID_CONN", "connection is closed")
        if op:
            self.host.hook(op)

    def getLibVersion(self):
        self._check("getLibVersion")
        return 12000000

    def isAlive(self):
        return int(self.alive and not self.host.down)

    def setKeepAlive(self, interval, count):
        if not self.lib.event_loop_registered:
            raise _err("VIR_ERR_INTERNAL_ERROR", "no event loop registered: cannot set keepalive")
        self.keepalive = (interval, count)
        return 0

    def registerCloseCallback(self, cb, opaque):
        self.close_cbs.append((cb, opaque))
        return 0

    def unregisterCloseCallback(self):
        if self.in_close_cb:
            raise AssertionError("re-entrant close/unregister inside close callback")
        self.close_cbs.clear()

    def close(self):
        if self.in_close_cb:
            raise AssertionError("re-entrant close/unregister inside close callback")
        self.close_count += 1
        self.closed_with_cb = bool(self.close_cbs)
        self.alive = False
        self.closed = True
        return 0

    def simulate_close(self, reason=CONSTS["VIR_CONNECT_CLOSE_REASON_KEEPALIVE"]):
        self.alive = False
        self.in_close_cb = True
        try:
            for cb, opaque in list(self.close_cbs):
                cb(self, reason, opaque)
        finally:
            self.in_close_cb = False

    def getInfo(self):
        self._check("getInfo")
        return list(self.host.info)

    def getCPUStats(self, cpu, flags=0):
        self._check("getCPUStats")
        return dict(self.host.cpu)

    def getMemoryStats(self, cell, flags=0):
        self._check("getMemoryStats")
        return dict(self.host.mem)

    def lookupByName(self, name):
        self._check("lookup")
        d = self.host.domains.get(name)
        if d is None:
            raise _err("VIR_ERR_NO_DOMAIN", f"Domain not found: no domain with matching name '{name}'")
        return d

    def listAllDomains(self, flags=0):
        self._check("listAllDomains")
        return list(self.host.domains.values())

    def defineXML(self, xml):
        self._check("define")
        name = ET.fromstring(xml).findtext("name")
        if name in self.host.domains:
            raise _err("VIR_ERR_DOM_EXIST", f"domain '{name}' already exists")
        d = FakeDomain(self.host, xml)
        self.host.domains[name] = d
        return d

    def storagePoolLookupByName(self, name):
        self._check()
        p = self.host.pools.get(name)
        if p is None:
            raise _err("VIR_ERR_NO_STORAGE_POOL", f"no pool {name}")
        return p

    def storagePoolDefineXML(self, xml, flags=0):
        self._check()
        root = ET.fromstring(xml)
        name = root.findtext("name")
        p = FakePool(self.host, name, root.findtext("target/path"))
        self.host.pools[name] = p
        return p

    def storageVolLookupByPath(self, path):
        self._check()
        for p in self.host.pools.values():
            if path in p.visible:
                return p.visible[path]
        raise _err("VIR_ERR_NO_STORAGE_VOL", f"Storage volume not found: no storage vol with matching path '{path}'")


class FakeLibvirtModule(types.ModuleType):
    libvirtError = libvirtError

    def __init__(self):
        super().__init__("libvirt")
        for k, v in {**ERR, **CONSTS}.items():
            setattr(self, k, v)
        self.event_loop_registered = False
        self.event_loop_runs = 0
        self.open_calls: list[str] = []
        self._hosts: dict[str, FakeHost] = {}

    def host(self, uri):
        if uri not in self._hosts:
            self._hosts[uri] = FakeHost(uri)
        return self._hosts[uri]

    def hang(self, uri, op="open"):
        ev = threading.Event()
        self.host(uri).hangs[op] = ev
        return ev

    def virEventRegisterDefaultImpl(self):
        self.event_loop_registered = True
        return 0

    def virEventRunDefaultImpl(self):
        self.event_loop_runs += 1
        threading.Event().wait(0.01)
        return 0

    def open(self, uri):
        self.open_calls.append(uri)
        h = self.host(uri)
        ev = h.hangs.get("open")
        if ev is not None:
            ev.wait()
        if h.down:
            raise _err("VIR_ERR_NO_CONNECT", f"ssh: connect to host {uri}: No route to host")
        c = FakeConn(self, h)
        h.conns.append(c)
        return c


@pytest.fixture
def stub_libvirt(monkeypatch):
    lib = FakeLibvirtModule()
    monkeypatch.setitem(sys.modules, "libvirt", lib)
    return lib
