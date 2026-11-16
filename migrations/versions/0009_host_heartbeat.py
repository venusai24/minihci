"""hosts: suspect/dead statuses and missed_heartbeats (FR-4.1, D-027)

Revision ID: 0009
Revises: 0008
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_OLD = "status in ('registering','ready','failed','removing','removed')"
_NEW = "status in ('registering','ready','suspect','dead','failed','removing','removed')"


def upgrade() -> None:
    op.add_column("hosts", sa.Column("missed_heartbeats", sa.Integer, nullable=False, server_default="0"))
    op.drop_constraint("ck_hosts_status", "hosts", type_="check")
    op.create_check_constraint("ck_hosts_status", "hosts", _NEW)


def downgrade() -> None:
    # suspect/dead hosts have no equivalent in the old schema; the host is unreachable, so call it failed
    op.execute("update hosts set status = 'failed' where status in ('suspect','dead')")
    op.drop_constraint("ck_hosts_status", "hosts", type_="check")
    op.create_check_constraint("ck_hosts_status", "hosts", _OLD)
    op.drop_column("hosts", "missed_heartbeats")
