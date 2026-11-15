from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Host(Base):
    __tablename__ = "hosts"
    __table_args__ = (
        CheckConstraint("status in ('registering','ready','failed')", name="ck_hosts_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    libvirt_uri: Mapped[str] = mapped_column(unique=True)
    cpu_total: Mapped[int | None]
    mem_total_mb: Mapped[int | None]
    status: Mapped[str] = mapped_column(default="registering", server_default="registering")
    last_heartbeat: Mapped[datetime | None]


class Vm(Base):
    __tablename__ = "vms"
    __table_args__ = (
        CheckConstraint("desired_state in ('running','stopped','absent')", name="ck_vms_desired_state"),
        CheckConstraint(
            "actual_state in ('pending','running','stopped','absent','error')", name="ck_vms_actual_state"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id"))
    vcpus: Mapped[int]
    mem_mb: Mapped[int]
    image: Mapped[str]
    disk_path: Mapped[str]
    desired_state: Mapped[str] = mapped_column(default="running", server_default="running")
    actual_state: Mapped[str] = mapped_column(default="pending", server_default="pending")


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "state in ('pending','running','succeeded','failed','rolled_back')", name="ck_tasks_state"
        ),
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
