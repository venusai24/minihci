"""vms.actual_state gains 'missing' (VM deleted outside MiniHCI)

Revision ID: 0004
Revises: 0003

Downgrade fails if any rows with actual_state='missing' exist (the old CHECK rejects
them); delete those rows or move them to another state first. See docs/decisions.md D-020.
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_vms_actual_state", "vms", type_="check")
    op.create_check_constraint(
        "ck_vms_actual_state", "vms", "actual_state in ('pending','running','stopped','absent','error','missing')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_vms_actual_state", "vms", type_="check")
    op.create_check_constraint(
        "ck_vms_actual_state", "vms", "actual_state in ('pending','running','stopped','absent','error')"
    )
