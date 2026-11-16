"""migrations: live-migration records with duration and downtime (FR-3.5, D-024)

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "migrations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Integer, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("vm_id", sa.Integer, sa.ForeignKey("vms.id"), nullable=False),
        sa.Column("src_host_id", sa.Integer, sa.ForeignKey("hosts.id"), nullable=False),
        sa.Column("dst_host_id", sa.Integer, sa.ForeignKey("hosts.id"), nullable=False),
        sa.Column("status", sa.String, nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("downtime_ms", sa.Integer),
        sa.UniqueConstraint("task_id", name="uq_migrations_task_id"),
        sa.CheckConstraint("status in ('running','succeeded','failed')", name="ck_migrations_status"),
        sa.CheckConstraint("duration_ms >= 0", name="ck_migrations_duration"),
        sa.CheckConstraint("downtime_ms >= 0", name="ck_migrations_downtime"),
    )


def downgrade() -> None:
    op.drop_table("migrations")
