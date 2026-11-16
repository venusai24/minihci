"""scheduler_config: rebalancer settings (FR-3.4, D-025)

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_CHECKS = {
    "ck_scheduler_config_rb_threshold": "rebalance_cpu_threshold_pct between 1 and 100",
    "ck_scheduler_config_rb_window": "rebalance_window_s >= 30",
    "ck_scheduler_config_rb_vm_cd": "rebalance_vm_cooldown_s >= 0",
    "ck_scheduler_config_rb_host_cd": "rebalance_host_cooldown_s >= 0",
}


def upgrade() -> None:
    op.add_column(
        "scheduler_config",
        sa.Column("rebalance_enabled", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    for name, default in [
        ("rebalance_cpu_threshold_pct", "80"),
        ("rebalance_window_s", "300"),
        ("rebalance_vm_cooldown_s", "1800"),
        ("rebalance_host_cooldown_s", "600"),
    ]:
        op.add_column("scheduler_config", sa.Column(name, sa.Integer, nullable=False, server_default=default))
    for name, expr in _CHECKS.items():
        op.create_check_constraint(name, "scheduler_config", expr)


def downgrade() -> None:
    for name in _CHECKS:
        op.drop_constraint(name, "scheduler_config", type_="check")
    for name in [
        "rebalance_host_cooldown_s",
        "rebalance_vm_cooldown_s",
        "rebalance_window_s",
        "rebalance_cpu_threshold_pct",
        "rebalance_enabled",
    ]:
        op.drop_column("scheduler_config", name)
