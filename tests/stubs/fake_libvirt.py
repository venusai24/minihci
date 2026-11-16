"""In-memory stand-in for the `libvirt` python module (no real connection ever).

Use the `stub_libvirt` fixture (tests/conftest.py); it injects an instance into
sys.modules['libvirt'] and yields it. Test controls live on the module object:
  lib.host(uri)              -> FakeHost (domains, pools, cpu/mem counters, down flag,
                                hangs/fail dicts keyed by op name)
  lib.hang(uri, op="open")   -> threading.Event; the op blocks until .set()
  lib.event_loop_registered  -> set by virEventRegisterDefaultImpl()
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

    def name(self):
        return self._name

    def info(self):  # [type, capacity, allocation]
        return [0, self.capacity, 0]

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


class FakeDomain:
    def __init__(self, host, xml):
        self.host = host
        self._name = ET.fromstring(xml).findtext("name")
        self.xml = xml
        self.state_ = SHUTOFF
        self.persistent = True
        self.defined = True
        self.job_stats: dict[str, int] | Exception = {"downtime": 90, "time_elapsed": 400}

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
        return [self.state_, 1]

    def isActive(self):
        self._check("isActive")
        return int(self.state_ == RUNNING)

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
        self._stop("destroy")

    def shutdown(self):
        self._stop("shutdown")

    def reboot(self, flags=0):
        self._check("reboot")
        if self.state_ != RUNNING:
            raise _err("VIR_ERR_OPERATION_INVALID", "domain is not running")

    def undefine(self):
        self._check("undefine")
        self.host.domains.pop(self._name, None)
        self.defined = False

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
