"""vms: actual_state gains 'paused' and 'crashed', plus state_reason (D-026 amendment 4)

Revision ID: 0011
Revises: 0010

state_reason holds libvirt's reason for a paused, crashed or shut-off domain (e.g. 'ioerror').
Downgrade maps paused/crashed rows to 'error' first, because the old CHECK rejects them.
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_OLD = "actual_state in ('pending','running','stopped','absent','error','missing','pending_restart')"
_NEW = "actual_state in ('pending','running','stopped','absent','error','missing','pending_restart','paused','crashed')"


def upgrade() -> None:
    op.add_column("vms", sa.Column("state_reason", sa.String))
    op.drop_constraint("ck_vms_actual_state", "vms", type_="check")
    op.create_check_constraint("ck_vms_actual_state", "vms", _NEW)


def downgrade() -> None:
    op.execute("update vms set actual_state = 'error' where actual_state in ('paused','crashed')")
    op.drop_constraint("ck_vms_actual_state", "vms", type_="check")
    op.create_check_constraint("ck_vms_actual_state", "vms", _OLD)
    op.drop_column("vms", "state_reason")
