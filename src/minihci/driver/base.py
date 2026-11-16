from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class DriverError(Exception): ...


class HostUnreachable(DriverError): ...


class DomainNotFound(DriverError): ...


class DomainExists(DriverError): ...


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
    downtime_ms: int


class DomainState(StrEnum):
    RUNNING = "running"
    SHUT_OFF = "shut_off"


@runtime_checkable
class HostDriver(Protocol):
    def probe_host(self, uri: str) -> HostInfo: ...
    def host_stats(self, uri: str) -> HostStats: ...  # HostUnreachable if unknown/down
    def list_domains(self, uri: str) -> dict[str, DomainState]: ...
    def domain_state(self, uri: str, name: str) -> DomainState | None: ...  # None = not defined
    def define_domain(self, uri: str, spec: DomainSpec) -> None: ...  # DomainExists if defined
    def undefine_domain(self, uri: str, name: str) -> None: ...  # DomainNotFound; DriverError if running
    def start_domain(self, uri: str, name: str) -> None: ...  # DriverError if already running
    def destroy_domain(self, uri: str, name: str) -> None: ...  # DriverError if not running
    def shutdown_domain(self, uri: str, name: str) -> None: ...  # graceful; DriverError if not running
    def reboot_domain(self, uri: str, name: str) -> None: ...  # DomainNotFound; DriverError if not running
    def disk_exists(self, path: str) -> bool: ...
    def create_disk_overlay(self, base_image: str, path: str) -> None: ...  # DriverError if path exists
    def delete_disk(self, path: str) -> None: ...  # DriverError if missing
    # DomainNotFound if not on src; DriverError if not running or name exists on dst;
    # HostUnreachable for unknown host. On any error the domain stays on src.
    def migrate_live(self, src_uri: str, dst_uri: str, name: str) -> MigrationResult: ...
