"""Store the active linked-console token encrypted for display.

Revision ID: f31a7c8d92b4
Revises: 12c679a45f10
Create Date: 2026-10-03 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "f31a7c8d92b4"
down_revision = "12c679a45f10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "node_access_tokens",
        sa.Column("token_ciphertext", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("node_access_tokens", "token_ciphertext")
