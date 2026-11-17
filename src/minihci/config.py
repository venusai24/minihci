from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MINIHCI_")

    database_url: str = "postgresql+psycopg://minihci:minihci@localhost:5433/minihci"
    driver: Literal["fake", "libvirt"] = "fake"
    api_token: str | None = None  # set it to require "Authorization: Bearer <token>" on every API route
    storage_root: str = "/srv/minihci/images"
    image_root: str = "/srv/minihci/base"
    dr_root: str = "/srv/minihci/dr"
    replication_tick_s: float = 5
    replication_retry_s: float = 60
    backup_timeout_s: float = 1800
    # D-026: hosts tried in order for disk operations. qemu:///system is the NFS server machine itself;
    # a deployment appends the qemu+ssh:// URIs of its hosts as fallbacks.
    libvirt_storage_uris: list[str] = ["qemu:///system"]
    libvirt_storage_timeout_s: float = 30  # per host, connect plus call
    fake_state_file: Path | None = None
    fake_delays: dict[str, float] = {}  # driver method -> seconds, slept BEFORE the effect
    fake_delays_after: dict[str, float] = {}  # slept AFTER the effect
    fake_fail: dict[str, int] = {}  # driver method -> times to raise DriverError
    worker_threads: int = 2
    worker_poll_interval_s: float = 0.2
    reconcile_interval_s: float = 30
    collector_interval_s: float = 10
    rebalance_interval_s: float = 30
    metrics_retention_h: float = 24
    heartbeat_interval_s: float = 5
    heartbeat_ping_timeout_s: float = 3
    heartbeat_suspect_misses: int = 3
    heartbeat_dead_after_s: float = 30
    ha_restart_max_attempts: int = 3  # failed restart tasks per VM per host outage, then it waits in pending_restart
    max_attempts: int = 3
    vm_shutdown_timeout_s: float = 60
