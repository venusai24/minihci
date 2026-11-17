from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, REAL
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Host(Base):
    __tablename__ = "hosts"
    __table_args__ = (
        CheckConstraint(
            "status in ('registering','ready','suspect','dead','failed','removing','removed')", name="ck_hosts_status"
        ),
        Index("uq_hosts_name_live", "name", unique=True, postgresql_where=text("status <> 'removed'")),
        Index("uq_hosts_uri_live", "libvirt_uri", unique=True, postgresql_where=text("status <> 'removed'")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    libvirt_uri: Mapped[str]
    cpu_total: Mapped[int | None]
    mem_total_mb: Mapped[int | None]
    status: Mapped[str] = mapped_column(default="registering", server_default="registering")
    last_heartbeat: Mapped[datetime | None]
    missed_heartbeats: Mapped[int] = mapped_column(default=0, server_default="0")


class Vm(Base):
    __tablename__ = "vms"
    __table_args__ = (
        CheckConstraint("desired_state in ('running','stopped','absent')", name="ck_vms_desired_state"),
        CheckConstraint(
            "actual_state in ('pending','running','stopped','absent','error','missing','pending_restart',"
            "'paused','crashed')",
            name="ck_vms_actual_state",
        ),
        Index("uq_vms_name_live", "name", unique=True, postgresql_where=text("desired_state <> 'absent'")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id"))
    vcpus: Mapped[int]
    mem_mb: Mapped[int]
    image: Mapped[str]
    disk_path: Mapped[str]
    desired_state: Mapped[str] = mapped_column(default="running", server_default="running")
    actual_state: Mapped[str] = mapped_column(default="pending", server_default="pending")
    state_reason: Mapped[str | None] = mapped_column(default=None)  # libvirt reason for paused/crashed (D-026)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint("state in ('pending','running','succeeded','failed','rolled_back')", name="ck_tasks_state"),
        UniqueConstraint("idempotency_key", name="uq_tasks_idempotency_key"),
        Index("ix_tasks_state_id", "state", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str]
    target_id: Mapped[int]
    state: Mapped[str] = mapped_column(default="pending", server_default="pending")
    steps: Mapped[list[Any]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    request_hash: Mapped[str | None]
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    error: Mapped[str | None]


class SchedulerConfig(Base):
    __tablename__ = "scheduler_config"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_scheduler_config_single_row"),
        CheckConstraint("cpu_headroom_pct between 1 and 100", name="ck_scheduler_config_headroom"),
        CheckConstraint("rebalance_cpu_threshold_pct between 1 and 100", name="ck_scheduler_config_rb_threshold"),
        CheckConstraint("rebalance_window_s >= 30", name="ck_scheduler_config_rb_window"),
        CheckConstraint("rebalance_vm_cooldown_s >= 0", name="ck_scheduler_config_rb_vm_cd"),
        CheckConstraint("rebalance_host_cooldown_s >= 0", name="ck_scheduler_config_rb_host_cd"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    cpu_headroom_pct: Mapped[int] = mapped_column(default=80, server_default="80")
    rebalance_enabled: Mapped[bool] = mapped_column(default=False, server_default=false())
    rebalance_cpu_threshold_pct: Mapped[int] = mapped_column(default=80, server_default="80")
    rebalance_window_s: Mapped[int] = mapped_column(default=300, server_default="300")
    rebalance_vm_cooldown_s: Mapped[int] = mapped_column(default=1800, server_default="1800")
    rebalance_host_cooldown_s: Mapped[int] = mapped_column(default=600, server_default="600")
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class HostMetric(Base):
    __tablename__ = "host_metrics"
    __table_args__ = (
        CheckConstraint("cpu_pct between 0 and 100", name="ck_host_metrics_cpu_pct"),
        CheckConstraint("mem_used_mb >= 0", name="ck_host_metrics_mem"),
        Index("ix_host_metrics_host_time", "host_id", "collected_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"))
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    cpu_pct: Mapped[float] = mapped_column(REAL)
    mem_used_mb: Mapped[int]


class Migration(Base):
    __tablename__ = "migrations"
    __table_args__ = (
        UniqueConstraint("task_id", name="uq_migrations_task_id"),
        CheckConstraint("status in ('running','succeeded','failed')", name="ck_migrations_status"),
        CheckConstraint("duration_ms >= 0", name="ck_migrations_duration"),
        CheckConstraint("downtime_ms >= 0", name="ck_migrations_downtime"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    vm_id: Mapped[int] = mapped_column(ForeignKey("vms.id"))
    src_host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id"))
    dst_host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id"))
    status: Mapped[str] = mapped_column(default="running", server_default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None]
    downtime_ms: Mapped[int | None]


class HaRestart(Base):
    __tablename__ = "ha_restarts"
    __table_args__ = (UniqueConstraint("task_id", name="uq_ha_restarts_task_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"))
    vm_id: Mapped[int] = mapped_column(ForeignKey("vms.id"))
    from_host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id"))
    to_host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id"))
    failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    running_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recovery_ms: Mapped[int | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
