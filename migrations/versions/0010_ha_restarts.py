"""vms.actual_state gains 'pending_restart'; ha_restarts table (FR-4.2, FR-4.4, D-028)

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_OLD = "actual_state in ('pending','running','stopped','absent','error','missing')"
_NEW = "actual_state in ('pending','running','stopped','absent','error','missing','pending_restart')"


def upgrade() -> None:
    op.drop_constraint("ck_vms_actual_state", "vms", type_="check")
    op.create_check_constraint("ck_vms_actual_state", "vms", _NEW)
    op.create_table(
        "ha_restarts",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Integer, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("vm_id", sa.Integer, sa.ForeignKey("vms.id"), nullable=False),
        sa.Column("from_host_id", sa.Integer, sa.ForeignKey("hosts.id"), nullable=False),
        sa.Column("to_host_id", sa.Integer, sa.ForeignKey("hosts.id"), nullable=False),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("running_at", sa.DateTime(timezone=True)),
        sa.Column("recovery_ms", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", name="uq_ha_restarts_task_id"),
    )


def downgrade() -> None:
    op.drop_table("ha_restarts")
    # the old schema has no pending_restart; the VM is not running anywhere, so call it error
    op.execute("update vms set actual_state = 'error' where actual_state = 'pending_restart'")
    op.drop_constraint("ck_vms_actual_state", "vms", type_="check")
    op.create_check_constraint("ck_vms_actual_state", "vms", _OLD)
