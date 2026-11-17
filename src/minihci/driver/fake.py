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

FAKE_DOWNTIME_MS = 30
FAKE_BLOCK_BYTES = 65536


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
        # incremental backups (since != None) to fail before touching anything: models a checkpoint libvirt still
        # lists whose qcow2 bitmap was not persisted (qemu killed), so an incremental from it fails every time
        self.fail_next_incremental = 0
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
            h = state["hosts"][uri]
        except KeyError:
            raise HostUnreachable(f"host unreachable: {uri}") from None
        if h.get("powered_off"):
            raise HostUnreachable(f"host powered off: {uri}")
        return h

    # ---- test helper (not part of the protocol) -------------------------
    def add_host(self, uri: str, cpu_total: int, mem_total_mb: int) -> None:
        with self._locked() as state:
            state["hosts"][uri] = {"cpu_total": cpu_total, "mem_total_mb": mem_total_mb, "domains": {}}

    def set_load(self, uri: str, cpu_pct: float, mem_used_mb: int | None = None) -> None:
        if not 0 <= cpu_pct <= 100:
            raise ValueError(f"cpu_pct out of range 0..100: {cpu_pct}")
        with self._locked() as state:
            self._host(state, uri)["load"] = {"cpu_pct": cpu_pct, "mem_used_mb": mem_used_mb}

    def power_off_host(self, uri: str) -> None:
        """Simulate a dead host: ping is False, every other op raises HostUnreachable. Domains are kept."""
        with self._locked() as state:
            self._raw_host(state, uri)["powered_off"] = True

    def power_on_host(self, uri: str) -> None:
        with self._locked() as state:
            self._raw_host(state, uri)["powered_off"] = False

    @staticmethod
    def _raw_host(state: dict[str, Any], uri: str) -> dict[str, Any]:
        try:
            return state["hosts"][uri]
        except KeyError:
            raise HostUnreachable(f"host unreachable: {uri}") from None

    # ---- HostDriver -----------------------------------------------------
    def ping(self, uri: str, timeout: float = 3) -> bool:
        def fn(state: dict[str, Any]) -> bool:
            h = state["hosts"].get(uri)
            return h is not None and not h.get("powered_off")

        return self._call("ping", fn)

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
                mem = sum(
                    d["mem_mb"] for d in h["domains"].values() if DomainState(d["state"]).active
                )  # a paused guest still holds its memory
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

    def domain_status(self, uri: str, name: str) -> DomainStatus | None:
        def fn(state: dict[str, Any]) -> DomainStatus | None:
            d = self._host(state, uri)["domains"].get(name)
            return DomainStatus(DomainState(d["state"]), d.get("reason")) if d else None

        return self._call("domain_status", fn)

    def set_domain_state(self, uri: str, name: str, new: DomainState, reason: str | None = None) -> None:
        """Test helper (not part of the protocol): put a domain into any state, e.g. PAUSED/ioerror."""

        def fn(state: dict[str, Any]) -> None:
            d = self._domain(state, uri, name)
            d["state"], d["reason"] = new.value, reason

        self._call("set_domain_state", fn)

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
            if DomainState(d["state"]).active:
                raise DriverError(f"cannot undefine active domain: {name}")
            del state["hosts"][uri]["domains"][name]  # its checkpoints go with it

        self._call("undefine_domain", fn)

    def start_domain(self, uri: str, name: str) -> None:
        def fn(state: dict[str, Any]) -> None:
            d = self._domain(state, uri, name)
            if DomainState(d["state"]).active:
                raise DriverError(f"domain already running: {name}")
            d["state"], d["reason"] = DomainState.RUNNING.value, None
            d["boot_count"] = d.get("boot_count", 0) + 1

        self._call("start_domain", fn)

    def shutdown_domain(self, uri: str, name: str) -> None:
        def fn(state: dict[str, Any]) -> None:
            d = self._domain(state, uri, name)
            if d["state"] != DomainState.RUNNING.value:
                raise DriverError(f"domain not running: {name}")
            d["state"] = DomainState.SHUT_OFF.value

        self._call("shutdown_domain", fn)

    def resume_domain(self, uri: str, name: str) -> None:
        def fn(state: dict[str, Any]) -> None:
            d = self._domain(state, uri, name)
            if d["state"] != DomainState.PAUSED.value:
                raise DriverError(f"domain not paused: {name}")
            d["state"], d["reason"] = DomainState.RUNNING.value, None

        self._call("resume_domain", fn)

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
            if not DomainState(d["state"]).active:
                raise DriverError(f"domain not running: {name}")
            d["state"], d["reason"] = DomainState.SHUT_OFF.value, "destroyed"

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
            state.get("blocks", {}).pop(path, None)

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
            moved = src["domains"].pop(name)
            moved.pop("checkpoints", None)  # probe Q4 (D-034): checkpoints do not travel with a live migration
            dst["domains"][name] = moved
            return MigrationResult(duration_ms=int((time.monotonic() - t0) * 1000), downtime_ms=FAKE_DOWNTIME_MS)

        return self._call("migrate_live", fn)

    # ---- checkpoints, push backups, block-map disks ---------------------
    # state["blocks"][path] = {str(block): version} (JSON keys are strings); state["seq"] is the version counter.
    @staticmethod
    def _read_disk(state: dict[str, Any], path: str) -> dict[int, int]:
        chain, seen, cur = [], set(), path
        while cur in state["disks"] and cur not in seen:  # a backing path outside `disks` contributes nothing
            seen.add(cur)
            chain.append(cur)
            cur = state["disks"][cur]
        out: dict[int, int] = {}
        for p in reversed(chain):  # base first, so the nearest overlay wins
            for b, v in state.get("blocks", {}).get(p, {}).items():
                out[int(b)] = v
        return out

    def read_disk(self, path: str) -> dict[int, int]:
        """Test helper (not part of the protocol): block -> version, backing chain resolved."""
        with self._locked() as state:
            return self._read_disk(state, path)

    def write_blocks(self, uri: str, name: str, blocks: list[int]) -> None:
        """Test helper (not part of the protocol): the guest writes to its disk."""

        def fn(state: dict[str, Any]) -> None:
            d = self._domain(state, uri, name)
            if d["state"] != DomainState.RUNNING.value:
                raise DriverError(f"domain not running: {name}")
            disk = state.setdefault("blocks", {}).setdefault(d["disk_path"], {})
            for b in blocks:
                state["seq"] = state.get("seq", 0) + 1
                disk[str(b)] = state["seq"]
            for dirty in d.get("checkpoints", {}).values():
                dirty.extend(b for b in blocks if b not in dirty)

        self._call("write_blocks", fn)

    def set_backup_job(self, uri: str, name: str, active: bool, abort_polls: int = 0) -> None:
        """Test helper (not part of the protocol): pretend a backup job is (not) running.

        abort_polls: after abort_backup the job stays visible for that many backup_active polls (like a real host).
        """

        def fn(state: dict[str, Any]) -> None:
            d = self._domain(state, uri, name)
            d["backup_job"] = active
            d["abort_polls"] = abort_polls
            d.pop("abort_left", None)

        self._call("set_backup_job", fn)

    def list_checkpoints(self, uri: str, name: str) -> list[str]:
        return self._call("list_checkpoints", lambda state: list(self._domain(state, uri, name).get("checkpoints", {})))

    def delete_checkpoint(self, uri: str, name: str, checkpoint: str) -> None:
        def fn(state: dict[str, Any]) -> None:
            d = self._domain(state, uri, name)
            if d.get("backup_job"):
                raise DriverError(f"backup job still active: {name}")  # like a real host mid-abort
            cps = d.get("checkpoints", {})
            if checkpoint not in cps:
                raise CheckpointNotFound(f"checkpoint not found: {checkpoint}")
            del cps[checkpoint]

        self._call("delete_checkpoint", fn)

    def backup_active(self, uri: str, name: str) -> bool:
        def fn(state: dict[str, Any]) -> bool:
            d = self._domain(state, uri, name)
            if d.get("abort_left", 0) > 0:
                d["abort_left"] -= 1
                if d["abort_left"] == 0:
                    d["backup_job"] = False
            return bool(d.get("backup_job"))

        return self._call("backup_active", fn)

    def abort_backup(self, uri: str, name: str) -> None:
        def fn(state: dict[str, Any]) -> None:
            d = self._domain(state, uri, name)
            if not d.get("backup_job") or d.get("abort_left", 0) > 0:
                raise DriverError(f"no backup job: {name}")
            if d.get("abort_polls", 0) > 0:
                d["abort_left"] = d["abort_polls"]  # still unwinding
            else:
                d["backup_job"] = False

        self._call("abort_backup", fn)

    def backup_domain(self, uri: str, name: str, target_path: str, checkpoint: str, since: str | None) -> BackupResult:
        t0 = time.monotonic()
        if since is not None and self.fail_next_incremental > 0:
            self.fail_next_incremental -= 1
            raise DriverError(f"injected failure: incremental backup since {since} (bitmap missing)")

        def fn(state: dict[str, Any]) -> BackupResult:
            d = self._domain(state, uri, name)
            if d["state"] != DomainState.RUNNING.value:
                raise DriverError(f"domain not running: {name}")
            if d.get("backup_job"):
                raise DriverError(f"backup job already active: {name}")
            cps = d.setdefault("checkpoints", {})
            if checkpoint in cps:
                raise DriverError(f"checkpoint exists: {checkpoint}")
            if target_path not in state["disks"]:
                raise DriverError(f"backup target missing: {target_path}")
            if since is not None and since not in cps:
                raise CheckpointNotFound(f"checkpoint not found: {since}")
            src = self._read_disk(state, d["disk_path"])
            wanted = src.keys() if since is None else {b for b in cps[since] if b in src}
            tgt = state.setdefault("blocks", {}).setdefault(target_path, {})
            for b in wanted:
                tgt[str(b)] = src[b]
            cps[checkpoint] = []
            return BackupResult(duration_ms=int((time.monotonic() - t0) * 1000))

        return self._call("backup_domain", fn)

    def create_blank_disk(self, path: str, like: str) -> None:
        def fn(state: dict[str, Any]) -> None:
            if path in state["disks"]:
                raise DriverError(f"disk exists: {path}")
            if like not in state["disks"]:
                raise DriverError(f"disk missing: {like}")
            state["disks"][path] = None
            state.setdefault("blocks", {})[path] = {}

        self._call("create_blank_disk", fn)

    def disk_allocation(self, path: str) -> int:
        def fn(state: dict[str, Any]) -> int:
            if path not in state["disks"]:
                raise DriverError(f"disk missing: {path}")
            return len(state.get("blocks", {}).get(path, {})) * FAKE_BLOCK_BYTES

        return self._call("disk_allocation", fn)
