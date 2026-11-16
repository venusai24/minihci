from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MINIHCI_")

    database_url: str = "postgresql+psycopg://minihci:minihci@localhost:5433/minihci"
    driver: Literal["fake", "libvirt"] = "fake"
    storage_root: str = "/srv/minihci/images"
    image_root: str = "/srv/minihci/base"
    fake_state_file: Path | None = None
    fake_delays: dict[str, float] = {}  # driver method -> seconds, slept BEFORE the effect
    fake_delays_after: dict[str, float] = {}  # slept AFTER the effect
    fake_fail: dict[str, int] = {}  # driver method -> times to raise DriverError
    worker_threads: int = 2
    worker_poll_interval_s: float = 0.2
    reconcile_interval_s: float = 30
    collector_interval_s: float = 10
    metrics_retention_h: float = 24
    max_attempts: int = 3
    vm_shutdown_timeout_s: float = 60
