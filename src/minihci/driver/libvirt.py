"""libvirt connection layer (D-026, FR-4.1 groundwork).

libvirt-python is imported lazily via load_libvirt(). The event loop must be running before any
connection is opened, otherwise setKeepAlive raises.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from types import ModuleType

from minihci.driver.base import (
    DomainExists,
    DomainNotFound,
    DomainSpec,
    DomainState,
    DriverError,
    HostInfo,
    HostStats,
    HostUnreachable,
    MigrationResult,
)
from minihci.driver.libvirt_errors import load_libvirt, map_error
from minihci.driver.libvirt_stats import cpu_pct, map_domain_state, mem_used_mb, migration_result
from minihci.driver.libvirt_xml import dir_pool_xml, domain_xml, overlay_volume_xml
from minihci.paths import resolve_image

log = logging.getLogger(__name__)

KEEPALIVE_INTERVAL = 5
KEEPALIVE_COUNT = 3

_loop_lock = threading.Lock()
_loop_lib: ModuleType | None = None
_loop_thread: threading.Thread | None = None


def start_event_loop() -> threading.Thread:
    """Register the default libvirt event impl and run it in a daemon thread. Idempotent."""
    global _loop_lib, _loop_thread
    lib = load_libvirt()
    with _loop_lock:
        if _loop_lib is lib and _loop_thread is not None and _loop_thread.is_alive():
            return _loop_thread
        lib.virEventRegisterDefaultImpl()
        _loop_lib = lib

        def run() -> None:
            while _loop_lib is lib:
                try:
                    lib.virEventRunDefaultImpl()
                except Exception:
                    log.exception("libvirt event loop iteration failed")
                    threading.Event().wait(0.1)

        _loop_thread = threading.Thread(target=run, name="libvirt-event-loop", daemon=True)
        _loop_thread.start()
        return _loop_thread


def stop_event_loop() -> None:
    """Ask the loop thread to exit (tests only; real libvirt cannot re-register an event impl)."""
    global _loop_lib, _loop_thread
    with _loop_lock:
        _loop_lib = None
        t, _loop_thread = _loop_thread, None
    if t is not None:
        t.join(timeout=1)


class LibvirtConnections:
    """Per-URI connection cache with keepalive, close eviction and a non-piling ping."""

    def __init__(self, on_close: Callable[[str], None] | None = None) -> None:
        self._on_close = on_close
        self._lock = threading.Lock()
        self._conns: dict[str, object] = {}
        self._pings: dict[str, threading.Thread] = {}
        self._to_close: list[object] = []

    def cached_uris(self) -> list[str]:
        with self._lock:
            return list(self._conns)

    def _drain_to_close(self) -> None:
        with self._lock:
            pending, self._to_close = self._to_close, []
        for c in pending:
            _quiet_close(c)

    def get(self, uri: str):
        self._drain_to_close()
        lib = load_libvirt()
        with self._lock:
            conn = self._conns.get(uri)
        if conn is not None:
            try:
                if conn.isAlive():
                    return conn
            except lib.libvirtError:
                pass
            self.evict(uri, conn)
        start_event_loop()
        new = lib.open(uri)  # may hang; never called under the lock
        try:
            new.setKeepAlive(KEEPALIVE_INTERVAL, KEEPALIVE_COUNT)
            new.registerCloseCallback(self._closed_cb(uri), uri)
        except BaseException:
            _quiet_close(new)
            raise
        with self._lock:
            existing = self._conns.get(uri)
            if existing is None:
                self._conns[uri] = new
                return new
        _quiet_close(new)  # lost a race with a concurrent opener
        return existing

    def _closed_cb(self, uri: str):
        def cb(conn, reason, opaque):
            with self._lock:
                evicted = self._conns.get(uri) is conn
                if evicted:
                    del self._conns[uri]
                    self._to_close.append(conn)  # closed later, outside the callback
            if evicted and self._on_close is not None:
                try:
                    self._on_close(uri)
                except Exception:
                    log.exception("on_close hook failed", extra={"uri": uri})

        return cb

    def evict(self, uri: str, conn: object) -> bool:
        """Remove conn only if the cache holds exactly it; close it and fire on_close. True if removed."""
        with self._lock:
            owned = self._conns.get(uri) is conn
            if owned:
                del self._conns[uri]
        if not owned:
            return False
        _quiet_close(conn)  # whoever removes a conn from the cache closes it
        if self._on_close is not None:
            try:
                self._on_close(uri)
            except Exception:
                log.exception("on_close hook failed", extra={"uri": uri})
        return True

    def drop(self, uri: str) -> None:
        """Forget the cached connection (call after a libvirt error); next get() reconnects."""
        self._drain_to_close()
        with self._lock:
            conn = self._conns.pop(uri, None)
        if conn is not None:
            _quiet_close(conn)
            if self._on_close is not None:
                try:
                    self._on_close(uri)
                except Exception:
                    log.exception("on_close hook failed", extra={"uri": uri})

    def ping(self, uri: str, timeout: float = 3) -> bool:
        """True if the host answers within timeout. A still-running earlier ping counts as a miss."""
        with self._lock:
            prev = self._pings.get(uri)
            if prev is not None and prev.is_alive():
                return False
            result: list[bool] = []

            def run() -> None:
                conn = None
                try:
                    conn = self.get(uri)
                    conn.getLibVersion()
                    result.append(True)
                except Exception:  # noqa: BLE001
                    if conn is not None:
                        self.evict(uri, conn)
                    result.append(False)

            t = threading.Thread(target=run, name=f"libvirt-ping-{uri}", daemon=True)
            self._pings[uri] = t
            t.start()
        t.join(timeout)
        return bool(result and result[0])


def _quiet_close(conn: object) -> None:
    with contextlib.suppress(Exception):
        conn.unregisterCloseCallback()
    try:
        conn.close()
    except Exception:
        log.debug("closing libvirt connection failed", exc_info=True)


class LibvirtDriver:
    """HostDriver host + domain operations over libvirt-python (D-026).

    Disk ops run on the first reachable host of `storage_uris` (D-026); pools are refreshed before
    every volume lookup because another host may have created or deleted the file.
    """

    IMAGES_POOL = "minihci-images"
    VMS_POOL = "minihci-vms"

    FIRST_SAMPLE_GAP_S = 0.5

    def __init__(
        self,
        conns: LibvirtConnections | None = None,
        sleep: Callable[[float], None] = time.sleep,
        storage_uris: list[str] | None = None,
        image_root: str = "/srv/minihci/base",
        storage_root: str = "/srv/minihci/images",
    ) -> None:
        self._storage_uris = list(storage_uris or [])
        self._image_root = image_root
        self._storage_root = storage_root
        self._sleep = sleep
        self._cpu_lock = threading.Lock()
        self._cpu_prev: dict[str, dict[str, int]] = {}
        if conns is None:
            conns = LibvirtConnections(on_close=self._forget_sample)
        elif conns._on_close is None:
            conns._on_close = self._forget_sample
        self._conns = conns

    def _forget_sample(self, uri: str) -> None:
        with self._cpu_lock:
            self._cpu_prev.pop(uri, None)

    def ping(self, uri: str, timeout: float = 3) -> bool:
        return self._conns.ping(uri, timeout)

    @contextlib.contextmanager
    def _op(self, uri: str):
        """Yield a live connection; map libvirt errors; drop the connection when the host looks gone."""
        lib = load_libvirt()
        try:
            conn = self._conns.get(uri)
        except lib.libvirtError as e:
            self._forget_sample(uri)
            raise HostUnreachable(str(e)) from e
        try:
            yield conn
        except lib.libvirtError as e:
            err = map_error(e, lib)
            if isinstance(err, HostUnreachable):
                self._conns.evict(uri, conn)
            raise err from e

    @staticmethod
    def _lookup(conn, name: str):
        return conn.lookupByName(name)  # NO_DOMAIN maps to DomainNotFound

    def probe_host(self, uri: str) -> HostInfo:
        with self._op(uri) as conn:
            info = conn.getInfo()  # [model, memory MiB, cpus, ...]
            return HostInfo(cpu_total=int(info[2]), mem_total_mb=int(info[1]))

    def host_stats(self, uri: str) -> HostStats:
        lib = load_libvirt()
        with self._op(uri) as conn:
            all_cpus = lib.VIR_NODE_CPU_STATS_ALL_CPUS
            with self._cpu_lock:
                prev = self._cpu_prev.get(uri)
            if prev is None:  # D-026: first call takes two samples so cpu_pct is a real number
                prev = dict(conn.getCPUStats(all_cpus))
                self._sleep(self.FIRST_SAMPLE_GAP_S)
            cur = dict(conn.getCPUStats(all_cpus))
            mem = dict(conn.getMemoryStats(lib.VIR_NODE_MEMORY_STATS_ALL_CELLS))
            with self._cpu_lock:
                self._cpu_prev[uri] = cur
            return HostStats(cpu_pct=cpu_pct(prev, cur), mem_used_mb=mem_used_mb(mem))

    def list_domains(self, uri: str) -> dict[str, DomainState]:
        out: dict[str, DomainState] = {}
        with self._op(uri) as conn:
            for dom in conn.listAllDomains(0):
                try:
                    out[dom.name()] = map_domain_state(dom.state(0)[0])
                except load_libvirt().libvirtError as e:
                    if not isinstance(map_error(e), DomainNotFound):
                        raise  # a domain that vanished mid-listing is simply skipped
        return out

    def domain_state(self, uri: str, name: str) -> DomainState | None:
        try:
            with self._op(uri) as conn:
                return map_domain_state(self._lookup(conn, name).state(0)[0])
        except DomainNotFound:
            return None

    def define_domain(self, uri: str, spec: DomainSpec) -> None:
        lib = load_libvirt()
        with self._op(uri) as conn:
            try:
                conn.lookupByName(spec.name)
            except lib.libvirtError as e:
                if not isinstance(map_error(e, lib), DomainNotFound):
                    raise
            else:
                raise DomainExists(f"domain {spec.name} already defined")
            conn.defineXML(domain_xml(spec))

    def undefine_domain(self, uri: str, name: str) -> None:
        with self._op(uri) as conn:
            dom = self._lookup(conn, name)
            if dom.isActive():  # libvirt would silently make it transient
                raise DriverError(f"domain {name} is running")
            dom.undefine()

    def start_domain(self, uri: str, name: str) -> None:
        # No timeout: create() can block for ~the NFS lease after a host death (probe results).
        with self._op(uri) as conn:
            self._lookup(conn, name).create()

    def destroy_domain(self, uri: str, name: str) -> None:
        with self._op(uri) as conn:
            self._lookup(conn, name).destroy()

    def shutdown_domain(self, uri: str, name: str) -> None:
        with self._op(uri) as conn:
            self._lookup(conn, name).shutdown()

    def reboot_domain(self, uri: str, name: str) -> None:
        with self._op(uri) as conn:
            self._lookup(conn, name).reboot(0)

    # ---- disks (D-026) ----

    def _disk_uri(self) -> str:
        for uri in self._storage_uris:
            if self.ping(uri):
                return uri
        raise HostUnreachable("no libvirt storage host answered")

    def _check_disk_path(self, path: str) -> None:
        if os.path.normpath(os.path.dirname(path)) != os.path.normpath(self._storage_root):
            raise DriverError(f"disk path must be directly under {self._storage_root}")

    def _ensure_pools(self, conn) -> list:
        lib = load_libvirt()
        pools = []
        for name, path in ((self.IMAGES_POOL, self._image_root), (self.VMS_POOL, self._storage_root)):
            try:
                pool = conn.storagePoolLookupByName(name)
            except lib.libvirtError as e:
                if e.get_error_code() != lib.VIR_ERR_NO_STORAGE_POOL:
                    raise
                pool = conn.storagePoolDefineXML(dir_pool_xml(name, path), 0)
                pool.setAutostart(1)
            actual = ET.fromstring(pool.XMLDesc(0)).findtext("target/path") or ""
            if os.path.normpath(actual) != os.path.normpath(path):  # D-026 (8): never redefine a pool
                raise DriverError(f"pool {name} targets {actual}, expected {path}")
            if not pool.isActive():
                pool.create(0)
            pools.append(pool)
        return pools

    def _find_volume(self, conn):
        """Return lookup(path) -> volume or None, after ensuring and refreshing the pools."""
        lib = load_libvirt()
        pools = self._ensure_pools(conn)
        for pool in pools:
            pool.refresh(0)  # the pool caches its volume list; another host may have changed the share

        def lookup(path: str):
            try:
                return conn.storageVolLookupByPath(path)
            except lib.libvirtError as e:
                if e.get_error_code() == lib.VIR_ERR_NO_STORAGE_VOL:
                    return None
                raise

        return pools, lookup

    def disk_exists(self, path: str) -> bool:
        with self._op(self._disk_uri()) as conn:
            _, lookup = self._find_volume(conn)
            return lookup(path) is not None

    def create_disk_overlay(self, base_image: str, path: str) -> None:
        try:
            base = resolve_image(base_image, self._image_root)  # D-014
        except ValueError as e:
            raise DriverError(f"invalid base image: {e}") from e
        self._check_disk_path(path)
        with self._op(self._disk_uri()) as conn:
            pools, lookup = self._find_volume(conn)
            if lookup(path) is not None:
                raise DriverError(f"disk {path} already exists")
            bvol = lookup(base)
            if bvol is None:
                raise DriverError(f"base image {base} not found")
            capacity = bvol.info()[1]
            root = ET.fromstring(bvol.XMLDesc(0))
            fmt = root.find("target/format")
            bfmt = fmt.get("type") if fmt is not None else "qcow2"
            pools[1].createXML(overlay_volume_xml(os.path.basename(path), capacity, base, bfmt), 0)

    def delete_disk(self, path: str) -> None:
        self._check_disk_path(path)
        with self._op(self._disk_uri()) as conn:
            _, lookup = self._find_volume(conn)
            vol = lookup(path)
            if vol is None:
                raise DriverError(f"disk {path} not found")
            vol.delete(0)

    # ---- migration (D-026, FR-3.2) ----

    def _evict_dead_or_both(self, pairs, lib) -> None:
        """After a connection-level migrate failure evict the conns that are dead; if none is, evict both."""
        dead = []
        for uri, conn in pairs:
            try:
                alive = bool(conn.isAlive())
            except lib.libvirtError:
                alive = False
            if not alive:
                dead.append((uri, conn))
        for uri, conn in dead or pairs:
            self._conns.evict(uri, conn)

    def migrate_live(self, src_uri: str, dst_uri: str, name: str) -> MigrationResult:
        lib = load_libvirt()
        flags = lib.VIR_MIGRATE_LIVE | lib.VIR_MIGRATE_PERSIST_DEST | lib.VIR_MIGRATE_UNDEFINE_SOURCE
        with self._op(src_uri) as sconn:
            dom = self._lookup(sconn, name)
            if not dom.isActive():
                raise DriverError(f"domain {name} is not running")
            with self._op(dst_uri) as dconn:
                try:
                    dconn.lookupByName(name)
                except lib.libvirtError as e:
                    if not isinstance(map_error(e, lib), DomainNotFound):
                        raise
                else:
                    raise DriverError(f"domain {name} already exists on destination")
                t0 = time.monotonic()
                try:
                    newdom = dom.migrate3(dconn, {}, flags)  # on error the domain stays on the source
                except lib.libvirtError as e:
                    err = map_error(e, lib)
                    if isinstance(err, HostUnreachable):
                        self._evict_dead_or_both(((src_uri, sconn), (dst_uri, dconn)), lib)
                    raise err from e
                wall_ms = int((time.monotonic() - t0) * 1000)
                try:  # stats come from the destination domain; the source is undefined by now
                    stats = newdom.jobStats(lib.VIR_DOMAIN_JOB_STATS_COMPLETED)
                except lib.libvirtError:
                    log.warning("migration job stats unavailable for %s", name, exc_info=True)
                    stats = None
                return migration_result(stats, wall_ms)
