"""core schema: hosts, vms, tasks

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hosts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False, unique=True),
        sa.Column("libvirt_uri", sa.String, nullable=False, unique=True),
        sa.Column("cpu_total", sa.Integer),
        sa.Column("mem_total_mb", sa.Integer),
        sa.Column("status", sa.String, nullable=False, server_default="registering"),
        sa.Column("last_heartbeat", sa.DateTime),
        sa.CheckConstraint("status in ('registering','ready','failed')", name="ck_hosts_status"),
    )
    op.create_table(
        "vms",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False, unique=True),
        sa.Column("host_id", sa.Integer, sa.ForeignKey("hosts.id"), nullable=False),
        sa.Column("vcpus", sa.Integer, nullable=False),
        sa.Column("mem_mb", sa.Integer, nullable=False),
        sa.Column("image", sa.String, nullable=False),
        sa.Column("disk_path", sa.String, nullable=False),
        sa.Column("desired_state", sa.String, nullable=False, server_default="running"),
        sa.Column("actual_state", sa.String, nullable=False, server_default="pending"),
        sa.CheckConstraint("desired_state in ('running','stopped','absent')", name="ck_vms_desired_state"),
        sa.CheckConstraint(
            "actual_state in ('pending','running','stopped','absent','error')", name="ck_vms_actual_state"
        ),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("type", sa.String, nullable=False),
        sa.Column("target_id", sa.Integer, nullable=False),
        sa.Column("state", sa.String, nullable=False, server_default="pending"),
        sa.Column("steps", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("params", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("idempotency_key", sa.String(255)),
        sa.Column("request_hash", sa.String),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime),
        sa.Column("finished_at", sa.DateTime),
        sa.Column("error", sa.Text),
        sa.CheckConstraint(
            "state in ('pending','running','succeeded','failed','rolled_back')", name="ck_tasks_state"
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_tasks_idempotency_key"),
    )
    op.create_index("ix_tasks_state_id", "tasks", ["state", "id"])


def downgrade() -> None:
    op.drop_index("ix_tasks_state_id", table_name="tasks")
    op.drop_table("tasks")
    op.drop_table("vms")
    op.drop_table("hosts")
