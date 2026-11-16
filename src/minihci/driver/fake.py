"""In-memory (optionally file-backed) driver. Deliberately NOT idempotent, like libvirt."""

import fcntl
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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

FAKE_DOWNTIME_MS = 30


class FakeDriver:
    def __init__(
        self,
        state_file: Path | None = None,
        delays: dict[str, float] | None = None,
        delays_after: dict[str, float] | None = None,
        fail: dict[str, int] | None = None,
    ) -> None:
        self.state_file = Path(state_file) if state_file else None
        self.delays: dict[str, float] = dict(delays or {})
        self.delays_after: dict[str, float] = dict(delays_after or {})
        self.fail: dict[str, int] = dict(fail or {})
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"hosts": {}, "disks": {}}

    # ---- plumbing -------------------------------------------------------
    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        """Hold the state lock; yield the state; persist it on exit if file-backed."""
        with self._lock:
            if self.state_file is None:
                yield self._state
                return
            lock_path = self.state_file.with_name(self.state_file.name + ".lock")
            with open(lock_path, "w") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                try:
                    state = (
                        json.loads(self.state_file.read_text())
                        if self.state_file.exists()
                        else {"hosts": {}, "disks": {}}
                    )
                    yield state
                    tmp = self.state_file.with_name(self.state_file.name + ".tmp")
                    tmp.write_text(json.dumps(state))
                    os.replace(tmp, self.state_file)
                finally:
                    fcntl.flock(lf, fcntl.LOCK_UN)

    def _call(self, method: str, fn: Callable[[dict[str, Any]], Any]) -> Any:
        if d := self.delays.get(method):
            time.sleep(d)
        if self.fail.get(method, 0) > 0:
            self.fail[method] -= 1
            raise DriverError(f"injected failure: {method}")
        with self._locked() as state:
            result = fn(state)
        if d := self.delays_after.get(method):
            time.sleep(d)
        return result

    @staticmethod
    def _host(state: dict[str, Any], uri: str) -> dict[str, Any]:
        try:
            return state["hosts"][uri]
        except KeyError:
            raise HostUnreachable(f"host unreachable: {uri}") from None

    # ---- test helper (not part of the protocol) -------------------------
    def add_host(self, uri: str, cpu_total: int, mem_total_mb: int) -> None:
        with self._locked() as state:
            state["hosts"][uri] = {"cpu_total": cpu_total, "mem_total_mb": mem_total_mb, "domains": {}}

    def set_load(self, uri: str, cpu_pct: float, mem_used_mb: int | None = None) -> None:
        if not 0 <= cpu_pct <= 100:
            raise ValueError(f"cpu_pct out of range 0..100: {cpu_pct}")
        with self._locked() as state:
            self._host(state, uri)["load"] = {"cpu_pct": cpu_pct, "mem_used_mb": mem_used_mb}

    # ---- HostDriver -----------------------------------------------------
    def probe_host(self, uri: str) -> HostInfo:
        def fn(state: dict[str, Any]) -> HostInfo:
            h = self._host(state, uri)
            return HostInfo(cpu_total=h["cpu_total"], mem_total_mb=h["mem_total_mb"])

        return self._call("probe_host", fn)

    def host_stats(self, uri: str) -> HostStats:
        def fn(state: dict[str, Any]) -> HostStats:
            h = self._host(state, uri)
            load = h.get("load") or {}
            mem = load.get("mem_used_mb")
            if mem is None:
                mem = sum(d["mem_mb"] for d in h["domains"].values() if d["state"] == DomainState.RUNNING.value)
            return HostStats(cpu_pct=float(load.get("cpu_pct", 0.0)), mem_used_mb=int(mem))

        return self._call("host_stats", fn)

    def list_domains(self, uri: str) -> dict[str, DomainState]:
        def fn(state: dict[str, Any]) -> dict[str, DomainState]:
            doms = self._host(state, uri)["domains"]
            return {n: DomainState(d["state"]) for n, d in doms.items()}

        return self._call("list_domains", fn)

    def domain_state(self, uri: str, name: str) -> DomainState | None:
        def fn(state: dict[str, Any]) -> DomainState | None:
            d = self._host(state, uri)["domains"].get(name)
            return DomainState(d["state"]) if d else None

        return self._call("domain_state", fn)

    def define_domain(self, uri: str, spec: DomainSpec) -> None:
        def fn(state: dict[str, Any]) -> None:
            doms = self._host(state, uri)["domains"]
            if spec.name in doms:
                raise DomainExists(f"domain exists: {spec.name}")
            doms[spec.name] = {
                "state": DomainState.SHUT_OFF.value,
                "vcpus": spec.vcpus,
                "mem_mb": spec.mem_mb,
                "disk_path": spec.disk_path,
            }

        self._call("define_domain", fn)

    def _domain(self, state: dict[str, Any], uri: str, name: str) -> dict[str, Any]:
        try:
            return self._host(state, uri)["domains"][name]
        except KeyError:
            raise DomainNotFound(f"domain not found: {name}") from None

    def undefine_domain(self, uri: str, name: str) -> None:
        def fn(state: dict[str, Any]) -> None:
            d = self._domain(state, uri, name)
            if d["state"] == DomainState.RUNNING.value:
                raise DriverError(f"cannot undefine running domain: {name}")
            del state["hosts"][uri]["domains"][name]

        self._call("undefine_domain", fn)

    def start_domain(self, uri: str, name: str) -> None:
        def fn(state: dict[str, Any]) -> None:
            d = self._domain(state, uri, name)
            if d["state"] == DomainState.RUNNING.value:
                raise DriverError(f"domain already running: {name}")
            d["state"] = DomainState.RUNNING.value
            d["boot_count"] = d.get("boot_count", 0) + 1

        self._call("start_domain", fn)

    def shutdown_domain(self, uri: str, name: str) -> None:
        def fn(state: dict[str, Any]) -> None:
            d = self._domain(state, uri, name)
            if d["state"] != DomainState.RUNNING.value:
                raise DriverError(f"domain not running: {name}")
            d["state"] = DomainState.SHUT_OFF.value

        self._call("shutdown_domain", fn)

    def reboot_domain(self, uri: str, name: str) -> None:
        def fn(state: dict[str, Any]) -> None:
            d = self._domain(state, uri, name)
            if d["state"] != DomainState.RUNNING.value:
                raise DriverError(f"domain not running: {name}")
            d["boot_count"] = d.get("boot_count", 0) + 1

        self._call("reboot_domain", fn)

    def boot_count(self, uri: str, name: str) -> int:
        """Test helper (not part of the protocol): how often the domain has booted."""
        with self._locked() as state:
            return self._domain(state, uri, name).get("boot_count", 0)

    def destroy_domain(self, uri: str, name: str) -> None:
        def fn(state: dict[str, Any]) -> None:
            d = self._domain(state, uri, name)
            if d["state"] != DomainState.RUNNING.value:
                raise DriverError(f"domain not running: {name}")
            d["state"] = DomainState.SHUT_OFF.value

        self._call("destroy_domain", fn)

    def disk_exists(self, path: str) -> bool:
        return self._call("disk_exists", lambda state: path in state["disks"])

    def create_disk_overlay(self, base_image: str, path: str) -> None:
        def fn(state: dict[str, Any]) -> None:
            if path in state["disks"]:
                raise DriverError(f"disk exists: {path}")
            state["disks"][path] = base_image

        self._call("create_disk_overlay", fn)

    def delete_disk(self, path: str) -> None:
        def fn(state: dict[str, Any]) -> None:
            if path not in state["disks"]:
                raise DriverError(f"disk missing: {path}")
            del state["disks"][path]

        self._call("delete_disk", fn)

    def migrate_live(self, src_uri: str, dst_uri: str, name: str) -> MigrationResult:
        t0 = time.monotonic()

        def fn(state: dict[str, Any]) -> MigrationResult:
            src = self._host(state, src_uri)
            dst = self._host(state, dst_uri)
            d = src["domains"].get(name)
            if d is None:
                raise DomainNotFound(f"domain not found: {name}")
            if d["state"] != DomainState.RUNNING.value:
                raise DriverError(f"domain not running: {name}")
            if name in dst["domains"]:
                raise DriverError(f"domain exists on destination: {name}")
            dst["domains"][name] = src["domains"].pop(name)
            return MigrationResult(duration_ms=int((time.monotonic() - t0) * 1000), downtime_ms=FAKE_DOWNTIME_MS)

        return self._call("migrate_live", fn)
