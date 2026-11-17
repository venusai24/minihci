from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class DriverError(Exception): ...


class HostUnreachable(DriverError): ...


class DomainNotFound(DriverError): ...


class DomainExists(DriverError): ...


class CheckpointNotFound(DriverError): ...


@dataclass(frozen=True)
class HostInfo:
    cpu_total: int
    mem_total_mb: int


@dataclass(frozen=True)
class HostStats:
    cpu_pct: float  # host-wide busy CPU, 0..100
    mem_used_mb: int


@dataclass(frozen=True)
class DomainSpec:
    name: str
    vcpus: int
    mem_mb: int
    disk_path: str


@dataclass(frozen=True)
class MigrationResult:
    duration_ms: int
    downtime_ms: int | None


@dataclass(frozen=True)
class BackupResult:
    duration_ms: int


class DomainState(StrEnum):
    RUNNING = "running"
    SHUT_OFF = "shut_off"
    PAUSED = "paused"  # D-026 (4): a paused guest still holds its memory and cannot be started again
    CRASHED = "crashed"  # the guest crashed and libvirt kept the (still active) domain

    @property
    def active(self) -> bool:
        """True while libvirt holds the domain open: it must be destroyed before it can be undefined."""
        return self is not DomainState.SHUT_OFF


@dataclass(frozen=True)
class DomainStatus:
    state: DomainState
    reason: str | None = None  # libvirt's reason for PAUSED, CRASHED and SHUT_OFF (D-026 (4))

    @property
    def unhealthy(self) -> bool:
        """A guest paused by a disk I/O error will not come back by itself."""
        return self.state is DomainState.PAUSED and self.reason == "ioerror"


@runtime_checkable
class HostDriver(Protocol):
    def ping(self, uri: str, timeout: float = 3) -> bool: ...  # True if the host answers within timeout
    def probe_host(self, uri: str) -> HostInfo: ...
    def host_stats(self, uri: str) -> HostStats: ...  # HostUnreachable if unknown/down
    def list_domains(self, uri: str) -> dict[str, DomainState]: ...
    def domain_state(self, uri: str, name: str) -> DomainState | None: ...  # None = not defined
    def domain_status(self, uri: str, name: str) -> DomainStatus | None: ...  # state + reason; None = not defined
    def define_domain(self, uri: str, spec: DomainSpec) -> None: ...  # DomainExists if defined
    def undefine_domain(self, uri: str, name: str) -> None: ...  # DomainNotFound; DriverError if active
    def start_domain(self, uri: str, name: str) -> None: ...  # DriverError if already running
    def destroy_domain(self, uri: str, name: str) -> None: ...  # DriverError if shut off (paused/crashed is fine)
    # shutdown_domain: graceful and asynchronous. Raises DomainNotFound if the domain is missing,
    # DriverError if not running; may return before the domain is actually off.
    def shutdown_domain(self, uri: str, name: str) -> None: ...
    def resume_domain(self, uri: str, name: str) -> None: ...  # DomainNotFound; DriverError if not paused
    def reboot_domain(self, uri: str, name: str) -> None: ...  # DomainNotFound; DriverError if not running
    def disk_exists(self, path: str) -> bool: ...
    def create_disk_overlay(self, base_image: str, path: str) -> None: ...  # DriverError if path exists
    def delete_disk(self, path: str) -> None: ...  # DriverError if missing
    # DomainNotFound if not on src; DriverError if not running or name exists on dst;
    # HostUnreachable for unknown host. On any error the domain stays on src.
    def migrate_live(self, src_uri: str, dst_uri: str, name: str) -> MigrationResult: ...
    def list_checkpoints(self, uri: str, name: str) -> list[str]: ...  # oldest first; DomainNotFound
    def delete_checkpoint(self, uri: str, name: str, checkpoint: str) -> None: ...  # DomainNotFound; CheckpointNotFound
    def backup_active(self, uri: str, name: str) -> bool: ...  # DomainNotFound
    def abort_backup(self, uri: str, name: str) -> None: ...  # DomainNotFound; DriverError if no backup job
    # Synchronous push backup of disk vda into the EXISTING qcow2 target_path, creating checkpoint `checkpoint`.
    # since=None: full; else incremental since that checkpoint. DomainNotFound; DriverError if not running,
    # if `checkpoint` already exists or target_path is missing; CheckpointNotFound if `since` is missing.
    # On any failure the new checkpoint does not exist afterwards.
    def backup_domain(
        self, uri: str, name: str, target_path: str, checkpoint: str, since: str | None
    ) -> BackupResult: ...
    # create_blank_disk: qcow2, capacity of `like`; DriverError if path exists or like missing
    def create_blank_disk(self, path: str, like: str) -> None: ...
    def disk_allocation(self, path: str) -> int: ...  # bytes allocated on storage; DriverError if missing
