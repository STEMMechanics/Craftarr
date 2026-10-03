"""Add remote Craftarr node links and per-user remote server assignments.

Revision ID: 12c679a45f10
Revises: 8d0a52f46c11
Create Date: 2026-10-03 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "12c679a45f10"
down_revision = "8d0a52f46c11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "node_access_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_node_access_tokens_singleton"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )

    op.create_table(
        "remote_nodes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("base_url", sa.String(length=1000), nullable=False),
        sa.Column("token_ciphertext", sa.Text(), nullable=False),
        sa.Column("last_connected_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("base_url"),
        sa.UniqueConstraint("name"),
        sa.UniqueConstraint("node_id"),
    )
    op.create_index(op.f("ix_remote_nodes_node_id"), "remote_nodes", ["node_id"], unique=False)

    op.create_table(
        "remote_servers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("node_id", sa.String(length=36), nullable=False),
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("minecraft_version", sa.String(length=40), nullable=True),
        sa.Column("paper_build", sa.String(length=40), nullable=True),
        sa.Column("memory", sa.String(length=20), nullable=False),
        sa.Column("min_memory", sa.String(length=20), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["node_id"], ["remote_nodes.node_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("node_id", "server_id", name="uq_remote_servers_node_server"),
    )
    op.create_index(op.f("ix_remote_servers_node_id"), "remote_servers", ["node_id"], unique=False)

    op.create_table(
        "user_remote_server_access",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("remote_server_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["remote_server_id"], ["remote_servers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "remote_server_id"),
    )


def downgrade() -> None:
    op.drop_table("user_remote_server_access")
    op.drop_index(op.f("ix_remote_servers_node_id"), table_name="remote_servers")
    op.drop_table("remote_servers")
    op.drop_index(op.f("ix_remote_nodes_node_id"), table_name="remote_nodes")
    op.drop_table("remote_nodes")
    op.drop_table("node_access_tokens")
