"""Add a persistent identity for each Craftarr instance.

Revision ID: 8d0a52f46c11
Revises: 4dbe1a7f2c90
Create Date: 2026-10-03 00:00:00.000000

"""
import uuid

from alembic import op
import sqlalchemy as sa


revision = "8d0a52f46c11"
down_revision = "4dbe1a7f2c90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "craftarr_instance",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_craftarr_instance_singleton"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_id"),
    )

    identity = sa.table(
        "craftarr_instance",
        sa.column("id", sa.Integer()),
        sa.column("node_id", sa.String(length=36)),
    )
    op.bulk_insert(
        identity,
        [{"id": 1, "node_id": str(uuid.uuid4())}],
    )


def downgrade() -> None:
    op.drop_table("craftarr_instance")
