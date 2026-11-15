"""vm names are unique only among live (non-absent) VMs

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("vms_name_key", "vms", type_="unique")
    op.create_index(
        "uq_vms_name_live", "vms", ["name"], unique=True, postgresql_where="desired_state <> 'absent'"
    )


def downgrade() -> None:
    op.drop_index("uq_vms_name_live", table_name="vms")
    op.create_unique_constraint("vms_name_key", "vms", ["name"])
