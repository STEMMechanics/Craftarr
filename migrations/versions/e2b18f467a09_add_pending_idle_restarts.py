"""Persist server restarts that wait for an empty player list."""

from alembic import op
import sqlalchemy as sa


revision = "e2b18f467a09"
down_revision = "45a6f1e2c903"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_idle_restarts",
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("requested_by_user_id", sa.Integer(), nullable=True),
        sa.Column("requested_by_username", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=255), server_default="Plugin update", nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("empty_since", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("server_id"),
    )


def downgrade() -> None:
    op.drop_table("pending_idle_restarts")
