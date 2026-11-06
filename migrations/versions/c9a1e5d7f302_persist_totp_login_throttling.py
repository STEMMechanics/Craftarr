"""Persist TOTP login throttling state.

Revision ID: c9a1e5d7f302
Revises: f4c8b9d2a1e7
"""
from alembic import op
import sqlalchemy as sa


revision = "c9a1e5d7f302"
down_revision = "f4c8b9d2a1e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("totp_failed_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "users",
        sa.Column("totp_locked_until", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "totp_locked_until")
    op.drop_column("users", "totp_failed_attempts")
