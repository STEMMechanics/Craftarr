"""Add opt-in Git branch builds for development servers."""
from alembic import op
import sqlalchemy as sa


revision = "c96d348a21f0"
down_revision = ("b47e2c9a61d0", "f03a8d8c12ab")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column("developer_updates_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "servers",
        sa.Column("developer_update_interval_minutes", sa.Integer(), nullable=False, server_default="5"),
    )
    op.add_column(
        "servers",
        sa.Column("developer_update_restart_policy", sa.String(length=20), nullable=False, server_default="manual"),
    )
    op.add_column(
        "plugin_monitoring_settings",
        sa.Column("development_branch", sa.String(length=255), nullable=False, server_default=""),
    )
    op.create_table(
        "developer_plugin_updates",
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("plugin_name", sa.String(length=200), nullable=False),
        sa.Column("source_key", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("latest_commit", sa.String(length=40), nullable=True),
        sa.Column("installed_commit", sa.String(length=40), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="waiting"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("build_log", sa.Text(), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("server_id", "plugin_name"),
    )


def downgrade() -> None:
    op.drop_table("developer_plugin_updates")
    op.drop_column("plugin_monitoring_settings", "development_branch")
    op.drop_column("servers", "developer_update_restart_policy")
    op.drop_column("servers", "developer_update_interval_minutes")
    op.drop_column("servers", "developer_updates_enabled")
