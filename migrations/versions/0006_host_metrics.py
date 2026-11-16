"""host_metrics: per-host CPU/memory samples (FR-3.3, D-023)

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "host_metrics",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("host_id", sa.Integer, sa.ForeignKey("hosts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("cpu_pct", sa.REAL, nullable=False),
        sa.Column("mem_used_mb", sa.Integer, nullable=False),
        sa.CheckConstraint("cpu_pct between 0 and 100", name="ck_host_metrics_cpu_pct"),
        sa.CheckConstraint("mem_used_mb >= 0", name="ck_host_metrics_mem"),
    )
    op.create_index("ix_host_metrics_host_time", "host_metrics", ["host_id", "collected_at"])


def downgrade() -> None:
    op.drop_index("ix_host_metrics_host_time", table_name="host_metrics")
    op.drop_table("host_metrics")
