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
from typing import TypeVar

from minihci.driver.base import (
    BackupResult,
    CheckpointNotFound,
    DomainExists,
    DomainNotFound,
    DomainSpec,
    DomainState,
    DomainStatus,
    DriverError,
    HostInfo,
    HostStats,
    HostUnreachable,
    MigrationResult,
)
from minihci.driver.libvirt_errors import load_libvirt, map_error
from minihci.driver.libvirt_stats import cpu_pct, map_domain_state, mem_used_mb, migration_result
from minihci.driver.libvirt_stats import domain_status as status_from_libvirt
from minihci.driver.libvirt_xml import (
    backup_xml,
    blank_volume_xml,
    checkpoint_xml,
    dir_pool_xml,
    domain_xml,
    overlay_volume_xml,
)
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


def _is_system_error(e: BaseException, lib: ModuleType) -> bool:
    return e.get_error_code() == getattr(lib, "VIR_ERR_SYSTEM_ERROR", object())


def _conn_alive(conn: object, lib: ModuleType) -> bool:
    try:
        return bool(conn.isAlive())
    except lib.libvirtError:
        return False


def _quiet_close(conn: object) -> None:
    with contextlib.suppress(Exception):
        conn.unregisterCloseCallback()
    try:
        conn.close()
    except Exception:
        log.debug("closing libvirt connection failed", exc_info=True)


T = TypeVar("T")


class _Precondition(DriverError):
    """The data, not the host, said no (disk exists, image or disk missing): another host would say the same."""


class LibvirtDriver:
    """HostDriver host + domain operations over libvirt-python (D-026).

    Disk ops try the hosts of `storage_uris` in order (D-026, amended): the next host is tried after any
    storage error or timeout on the previous one, but not after a precondition failure (disk exists,
    base image missing), because the share is common to all hosts and the answer would be the same.
    Pools are refreshed before every volume lookup because another host may have changed the share.
    """

    IMAGES_POOL = "minihci-images"
    VMS_POOL = "minihci-vms"
    DR_POOL = "minihci-dr"

    BACKUP_POLL_S = 0.5

    FIRST_SAMPLE_GAP_S = 0.5

    def __init__(
        self,
        conns: LibvirtConnections | None = None,
        sleep: Callable[[float], None] = time.sleep,
        storage_uris: list[str] | None = None,
        image_root: str = "/srv/minihci/base",
        storage_root: str = "/srv/minihci/images",
        storage_timeout_s: float = 30,
        dr_root: str = "/srv/minihci/dr",
        backup_timeout_s: float = 1800,
    ) -> None:
        self._dr_root = dr_root
        self._backup_timeout_s = backup_timeout_s
        self._storage_uris = list(storage_uris or [])
        self._storage_timeout_s = storage_timeout_s
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
            if isinstance(err, HostUnreachable) and _is_system_error(e, lib) and _conn_alive(conn, lib):
                # SYSTEM_ERROR is also what a failed disk open or permission error looks like; with the
                # connection still alive the host is reachable and this is an ordinary operation failure.
                err = DriverError(str(e))
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
        status = self.domain_status(uri, name)
        return None if status is None else status.state

    def domain_status(self, uri: str, name: str) -> DomainStatus | None:
        """State plus libvirt's reason (paused: user, ioerror, ...), D-026 (4)."""
        try:
            with self._op(uri) as conn:
                return status_from_libvirt(self._lookup(conn, name).state(0))
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
        lib = load_libvirt()
        with self._op(uri) as conn:
            dom = self._lookup(conn, name)
            if dom.isActive():  # libvirt would silently make it transient
                raise DriverError(f"domain {name} is running")
            flag = getattr(lib, "VIR_DOMAIN_UNDEFINE_CHECKPOINTS_METADATA", None)
            if flag is None:
                dom.undefine()
            else:
                dom.undefineFlags(flag)  # checkpoint metadata would otherwise make undefine fail

    def start_domain(self, uri: str, name: str) -> None:
        # No timeout: create() can block for ~the NFS lease after a host death (probe results).
        with self._op(uri) as conn:
            self._lookup(conn, name).create()

    def resume_domain(self, uri: str, name: str) -> None:
        with self._op(uri) as conn:
            self._lookup(conn, name).resume()

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

    def _storage(self, fn: Callable[[object], T]) -> T:
        """Run fn(conn) on the storage hosts in order until one succeeds (D-026 amended).

        Each attempt gets `storage_timeout_s` for connecting plus the call. A timeout, an unreachable
        host or any other storage error moves on to the next host; a _Precondition does not.
        """
        if not self._storage_uris:
            raise HostUnreachable("no libvirt storage host configured")
        failures: list[DriverError] = []
        for uri in self._storage_uris:
            try:
                return self._attempt(uri, fn)
            except _Precondition:
                raise
            except DriverError as e:  # includes HostUnreachable
                failures.append(e)
                log.warning(
                    "storage host failed, trying the next one",
                    extra={"fields": {"event": "storage_fallback", "uri": uri, "error": str(e)}},
                )
        hard = [e for e in failures if not isinstance(e, HostUnreachable)]
        raise (hard or failures)[0]

    def _attempt(self, uri: str, fn: Callable[[object], T]) -> T:
        out: list[tuple[bool, object]] = []

        def run() -> None:
            try:
                with self._op(uri) as conn:
                    out.append((True, fn(conn)))
            except BaseException as e:  # noqa: BLE001 - re-raised in the caller
                out.append((False, e))

        t = threading.Thread(target=run, name=f"libvirt-storage-{uri}", daemon=True)
        t.start()
        t.join(self._storage_timeout_s)
        if not out:  # still running: drop the connection so the next attempt starts clean
            self._conns.drop(uri)
            raise HostUnreachable(f"storage call on {uri} timed out after {self._storage_timeout_s}s")
        ok, value = out[0]
        if not ok:
            raise value  # type: ignore[misc]
        return value  # type: ignore[return-value]

    @staticmethod
    def _dir(path: str) -> str:
        return os.path.normpath(os.path.dirname(path))

    def _in_dr(self, *paths: str) -> bool:
        return any(self._dir(p) == os.path.normpath(self._dr_root) for p in paths)

    def _check_disk_path(self, path: str) -> None:
        if self._dir(path) not in (os.path.normpath(self._storage_root), os.path.normpath(self._dr_root)):
            raise DriverError(f"disk path must be directly under {self._storage_root} or {self._dr_root}")

    def _ensure_pools(self, conn, dr: bool = False) -> list:
        """Image and VM pools, plus the DR pool when `dr` (created on demand, same path rule)."""
        lib = load_libvirt()
        wanted = [(self.IMAGES_POOL, self._image_root), (self.VMS_POOL, self._storage_root)]
        if dr:
            wanted.append((self.DR_POOL, self._dr_root))
        pools = []
        for name, path in wanted:
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

    def _pool_for(self, pools: list, path: str):
        """The pool that holds new volumes at `path` (storage_root or dr_root)."""
        return pools[2] if self._dir(path) == os.path.normpath(self._dr_root) else pools[1]

    def _find_volume(self, conn, dr: bool = False):
        """Return lookup(path) -> volume or None, after ensuring and refreshing the pools."""
        lib = load_libvirt()
        pools = self._ensure_pools(conn, dr)
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
        def run(conn) -> bool:
            _, lookup = self._find_volume(conn, self._in_dr(path))
            return lookup(path) is not None

        return self._storage(run)

    def create_disk_overlay(self, base_image: str, path: str) -> None:
        try:
            base = resolve_image(base_image, self._image_root)  # D-014
        except ValueError as e:
            try:
                base = resolve_image(base_image, self._dr_root)  # a DR replica chain overlays DR disks
            except ValueError:
                raise DriverError(f"invalid base image: {e}") from e
        self._check_disk_path(path)

        def run(conn) -> None:
            pools, lookup = self._find_volume(conn, self._in_dr(path, base))
            if lookup(path) is not None:
                raise _Precondition(f"disk {path} already exists")
            bvol = lookup(base)
            if bvol is None:
                raise _Precondition(f"base image {base} not found")
            capacity = bvol.info()[1]
            root = ET.fromstring(bvol.XMLDesc(0))
            fmt = root.find("target/format")
            bfmt = fmt.get("type") if fmt is not None else "qcow2"
            self._pool_for(pools, path).createXML(overlay_volume_xml(os.path.basename(path), capacity, base, bfmt), 0)

        self._storage(run)

    def delete_disk(self, path: str) -> None:
        self._check_disk_path(path)

        def run(conn) -> None:
            _, lookup = self._find_volume(conn, self._in_dr(path))
            vol = lookup(path)
            if vol is None:
                raise _Precondition(f"disk {path} not found")
            vol.delete(0)

        self._storage(run)

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

    # ---- backups and DR disks (M5, D-034) ----

    def create_blank_disk(self, path: str, like: str) -> None:
        self._check_disk_path(path)

        def run(conn) -> None:
            pools, lookup = self._find_volume(conn, self._in_dr(path, like))
            if lookup(path) is not None:
                raise _Precondition(f"disk {path} already exists")
            lvol = lookup(like)
            if lvol is None:
                raise _Precondition(f"disk {like} not found")
            self._pool_for(pools, path).createXML(blank_volume_xml(os.path.basename(path), lvol.info()[1]), 0)

        self._storage(run)

    def disk_allocation(self, path: str) -> int:
        self._check_disk_path(path)

        def run(conn) -> int:
            _, lookup = self._find_volume(conn, self._in_dr(path))
            vol = lookup(path)
            if vol is None:
                raise _Precondition(f"disk {path} not found")
            return int(vol.info()[2])

        return self._storage(run)

    @staticmethod
    def _sorted_checkpoints(dom) -> list[str]:
        rows = []
        for cp in dom.listAllCheckpoints(0):
            ctime = int(ET.fromstring(cp.getXMLDesc(0)).findtext("creationTime") or 0)
            rows.append((ctime, cp.name()))
        return [n for _, n in sorted(rows)]

    def list_checkpoints(self, uri: str, name: str) -> list[str]:
        with self._op(uri) as conn:
            return self._sorted_checkpoints(self._lookup(conn, name))

    def delete_checkpoint(self, uri: str, name: str, checkpoint: str) -> None:
        lib = load_libvirt()
        with self._op(uri) as conn:
            dom = self._lookup(conn, name)
            try:
                dom.checkpointLookupByName(checkpoint, 0).delete(0)
            except lib.libvirtError as e:
                if e.get_error_code() == getattr(lib, "VIR_ERR_NO_DOMAIN_CHECKPOINT", object()):
                    raise CheckpointNotFound(f"checkpoint {checkpoint} of {name} not found") from e
                raise

    def backup_active(self, uri: str, name: str) -> bool:
        lib = load_libvirt()
        with self._op(uri) as conn:
            dom = self._lookup(conn, name)
            if dom.jobInfo()[0] == lib.VIR_DOMAIN_JOB_NONE:
                return False
            return dom.jobStats(0).get("operation") == lib.VIR_DOMAIN_JOB_OPERATION_BACKUP

    def abort_backup(self, uri: str, name: str) -> None:
        lib = load_libvirt()
        with self._op(uri) as conn:
            dom = self._lookup(conn, name)
            if dom.jobInfo()[0] == lib.VIR_DOMAIN_JOB_NONE or (
                dom.jobStats(0).get("operation") != lib.VIR_DOMAIN_JOB_OPERATION_BACKUP
            ):
                raise DriverError(f"no backup job on {name}")
            dom.abortJob()

    ABORT_WAIT_POLLS = 20

    def _wait_job_gone(self, dom) -> None:
        """After abortJob() the job may take a few polls to disappear; the checkpoint is only safe to drop then."""
        lib = load_libvirt()
        for _ in range(self.ABORT_WAIT_POLLS):
            if dom.jobInfo()[0] == lib.VIR_DOMAIN_JOB_NONE:
                return
            self._sleep(self.BACKUP_POLL_S)

    def _drop_checkpoint(self, dom, checkpoint: str) -> bool:
        """Delete a failed backup's new checkpoint (one retry). True if it no longer exists afterwards."""
        for attempt in range(2):
            try:
                if checkpoint not in self._sorted_checkpoints(dom):
                    return True
                dom.checkpointLookupByName(checkpoint, 0).delete(0)
            except Exception:
                log.warning("could not delete checkpoint %s of a failed backup", checkpoint, exc_info=True)
            if attempt == 0:
                self._sleep(self.BACKUP_POLL_S)
        try:
            return checkpoint not in self._sorted_checkpoints(dom)
        except Exception:  # noqa: BLE001
            return False

    def _cleanup_failed(self, dom, name: str, checkpoint: str, err: BaseException) -> None:
        if not self._drop_checkpoint(dom, checkpoint):
            raise DriverError(f"backup of {name} failed ({err}) and checkpoint {checkpoint} is still present") from err

    def backup_domain(self, uri: str, name: str, target_path: str, checkpoint: str, since: str | None) -> BackupResult:
        lib = load_libvirt()
        self._check_disk_path(target_path)
        if not self.disk_exists(target_path):
            raise DriverError(f"backup target {target_path} not found")
        with self._op(uri) as conn:
            dom = self._lookup(conn, name)
            if not dom.isActive():
                raise DriverError(f"domain {name} is not running")
            if checkpoint in self._sorted_checkpoints(dom):
                raise DriverError(f"checkpoint {checkpoint} already exists on {name}")
            t0 = time.monotonic()
            try:
                dom.backupBegin(
                    backup_xml(target_path, since),
                    checkpoint_xml(checkpoint),
                    lib.VIR_DOMAIN_BACKUP_BEGIN_REUSE_EXTERNAL,
                )
            except lib.libvirtError as e:
                self._cleanup_failed(dom, name, checkpoint, e)
                if since is not None and e.get_error_code() == getattr(lib, "VIR_ERR_NO_DOMAIN_CHECKPOINT", object()):
                    raise CheckpointNotFound(f"checkpoint {since} of {name} not found") from e
                raise
            try:
                while dom.jobInfo()[0] != lib.VIR_DOMAIN_JOB_NONE:
                    if time.monotonic() - t0 >= self._backup_timeout_s:
                        with contextlib.suppress(lib.libvirtError):
                            dom.abortJob()
                            self._wait_job_gone(dom)
                        raise DriverError(f"backup of {name} timed out after {self._backup_timeout_s}s")
                    self._sleep(self.BACKUP_POLL_S)
                stats = dom.jobStats(lib.VIR_DOMAIN_JOB_STATS_COMPLETED | lib.VIR_DOMAIN_JOB_STATS_KEEP_COMPLETED)
                if stats.get("type") != lib.VIR_DOMAIN_JOB_COMPLETED:
                    raise DriverError(f"backup of {name} did not complete (job type {stats.get('type')})")
            except BaseException as e:
                self._cleanup_failed(dom, name, checkpoint, e)
                raise
            return BackupResult(duration_ms=int((time.monotonic() - t0) * 1000))
