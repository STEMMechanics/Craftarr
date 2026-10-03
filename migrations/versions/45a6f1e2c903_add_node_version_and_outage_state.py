"""Track linked Node versions and delayed outage alerts.

Revision ID: 45a6f1e2c903
Revises: f31a7c8d92b4
Create Date: 2026-10-04 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "45a6f1e2c903"
down_revision = "f31a7c8d92b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("remote_nodes", sa.Column("app_version", sa.String(length=40), nullable=True))
    op.add_column("remote_nodes", sa.Column("outage_started_at", sa.DateTime(), nullable=True))
    op.add_column(
        "remote_nodes",
        sa.Column("offline_alert_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("remote_nodes", "offline_alert_sent")
    op.drop_column("remote_nodes", "outage_started_at")
    op.drop_column("remote_nodes", "app_version")
