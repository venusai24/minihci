"""soft-removed hosts: statuses removing/removed, name/uri unique only among live hosts

Revision ID: 0003
Revises: 0002

Downgrade fails if any removing/removed rows exist (the old CHECK rejects them); delete
those rows (and their absent VMs) first. See docs/decisions.md D-015.
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_LIVE = "status <> 'removed'"


def upgrade() -> None:
    op.drop_constraint("ck_hosts_status", "hosts", type_="check")
    op.create_check_constraint(
        "ck_hosts_status", "hosts", "status in ('registering','ready','failed','removing','removed')"
    )
    op.drop_constraint("hosts_name_key", "hosts", type_="unique")
    op.drop_constraint("hosts_libvirt_uri_key", "hosts", type_="unique")
    op.create_index("uq_hosts_name_live", "hosts", ["name"], unique=True, postgresql_where=_LIVE)
    op.create_index("uq_hosts_uri_live", "hosts", ["libvirt_uri"], unique=True, postgresql_where=_LIVE)


def downgrade() -> None:
    op.drop_index("uq_hosts_uri_live", table_name="hosts")
    op.drop_index("uq_hosts_name_live", table_name="hosts")
    op.drop_constraint("ck_hosts_status", "hosts", type_="check")
    op.create_check_constraint("ck_hosts_status", "hosts", "status in ('registering','ready','failed')")
    op.create_unique_constraint("hosts_name_key", "hosts", ["name"])
    op.create_unique_constraint("hosts_libvirt_uri_key", "hosts", ["libvirt_uri"])
