"""Add per-server audit history."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4dbe1a7f2c90"
down_revision: Union[str, Sequence[str], None] = "7dcb8a9016fe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "server_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("server_name", sa.String(length=100), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("actor_username", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=255), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_server_audit_events_server_id", "server_audit_events", ["server_id"])
    op.create_index("ix_server_audit_events_actor_user_id", "server_audit_events", ["actor_user_id"])
    op.create_index("ix_server_audit_events_created_at", "server_audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_server_audit_events_created_at", table_name="server_audit_events")
    op.drop_index("ix_server_audit_events_actor_user_id", table_name="server_audit_events")
    op.drop_index("ix_server_audit_events_server_id", table_name="server_audit_events")
    op.drop_table("server_audit_events")
