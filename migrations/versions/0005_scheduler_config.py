"""scheduler_config: single-row table holding placement settings (FR-3.2, D-022)

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduler_config",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("cpu_headroom_pct", sa.Integer, nullable=False, server_default="80"),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_scheduler_config_single_row"),
        sa.CheckConstraint("cpu_headroom_pct between 1 and 100", name="ck_scheduler_config_headroom"),
    )
    op.execute("INSERT INTO scheduler_config (id, cpu_headroom_pct) VALUES (1, 80)")


def downgrade() -> None:
    op.drop_table("scheduler_config")
