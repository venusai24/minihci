"""DR replication: hosts.site, protection_policies, replicas, dr_failovers (D-033..D-037)

Revision ID: 0012
Revises: 0011

replicas.chain_id, dr_path, checkpoint and snapshot_time are nullable: the replicate task inserts the row
first and fills them from the row id in the same transaction.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import REAL

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("hosts", sa.Column("site", sa.String, nullable=False, server_default="primary"))
    op.create_check_constraint("ck_hosts_site", "hosts", "site in ('primary','dr')")

    op.create_table(
        "protection_policies",
        sa.Column("vm_id", sa.Integer, sa.ForeignKey("vms.id"), primary_key=True),
        sa.Column("interval_s", sa.Integer, nullable=False),
        sa.Column("retention", sa.Integer, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("interval_s between 30 and 86400", name="ck_protection_policies_interval"),
        sa.CheckConstraint("retention between 1 and 100", name="ck_protection_policies_retention"),
    )

    op.create_table(
        "replicas",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("vm_id", sa.Integer, sa.ForeignKey("vms.id"), nullable=False),
        sa.Column("task_id", sa.Integer, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("host_id", sa.Integer, sa.ForeignKey("hosts.id"), nullable=False),
        sa.Column("mode", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False, server_default="running"),
        sa.Column("chain_id", sa.BigInteger),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("dr_path", sa.String),
        sa.Column("checkpoint", sa.String),
        sa.Column("since_checkpoint", sa.String),
        sa.Column("backing_path", sa.String),
        sa.Column("snapshot_time", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("size_bytes", sa.BigInteger),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("task_id", name="uq_replicas_task_id"),
        sa.CheckConstraint("mode in ('full','incremental')", name="ck_replicas_mode"),
        sa.CheckConstraint("status in ('running','succeeded','failed','pruned')", name="ck_replicas_status"),
        sa.CheckConstraint("seq >= 0", name="ck_replicas_seq"),
    )
    op.create_index("ix_replicas_vm_status_time", "replicas", ["vm_id", "status", "snapshot_time"])

    op.create_table(
        "dr_failovers",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.Integer, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("vm_id", sa.Integer, sa.ForeignKey("vms.id"), nullable=False),
        sa.Column("replica_id", sa.BigInteger, sa.ForeignKey("replicas.id"), nullable=False),
        sa.Column("from_host_id", sa.Integer, sa.ForeignKey("hosts.id"), nullable=False),
        sa.Column("dr_host_id", sa.Integer, sa.ForeignKey("hosts.id"), nullable=False),
        sa.Column("from_disk_path", sa.String, nullable=False),
        sa.Column("disk_path", sa.String, nullable=False),
        sa.Column("forced", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("primary_failed_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("running_at", sa.DateTime(timezone=True)),
        sa.Column("failover_ms", sa.Integer),
        sa.Column("data_loss_s", REAL),
        sa.UniqueConstraint("task_id", name="uq_dr_failovers_task_id"),
    )


def downgrade() -> None:
    op.drop_table("dr_failovers")
    op.drop_index("ix_replicas_vm_status_time", table_name="replicas")
    op.drop_table("replicas")
    op.drop_table("protection_policies")
    op.drop_constraint("ck_hosts_site", "hosts", type_="check")
    op.drop_column("hosts", "site")
