"""libvirt connection layer (D-026, FR-4.1 groundwork).

libvirt-python is imported lazily via load_libvirt(). The event loop must be running before any
connection is opened, otherwise setKeepAlive raises.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from types import ModuleType

from minihci.driver.libvirt_errors import load_libvirt

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
            self._evict(uri, conn)
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

    def _evict(self, uri: str, conn: object) -> None:
        with self._lock:
            owned = self._conns.get(uri) is conn
            if owned:
                del self._conns[uri]
        if owned:  # whoever removes a conn from the cache closes it
            _quiet_close(conn)

    def drop(self, uri: str) -> None:
        """Forget the cached connection (call after a libvirt error); next get() reconnects."""
        self._drain_to_close()
        with self._lock:
            conn = self._conns.pop(uri, None)
        if conn is not None:
            _quiet_close(conn)

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
                        self._evict(uri, conn)
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
