"""Add per-user auth session generations.

Revision ID: b7e5d8214c39
Revises: c9a1e5d7f302
"""
from alembic import op
import sqlalchemy as sa


revision = "b7e5d8214c39"
down_revision = "c9a1e5d7f302"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "auth_session_generation",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "auth_session_generation")
