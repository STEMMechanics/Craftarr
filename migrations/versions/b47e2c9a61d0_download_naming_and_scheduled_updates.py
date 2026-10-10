"""Rename download settings and add queued automatic updates."""
from alembic import op
import sqlalchemy as sa


revision = "b47e2c9a61d0"
down_revision = "a13d7c9e5b20"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("plugin_monitoring_settings") as batch:
        batch.alter_column("link_pattern", new_column_name="download_url")
        batch.add_column(sa.Column("download_rename", sa.String(length=255), nullable=False, server_default=""))

    op.create_table(
        "pending_automatic_updates",
        sa.Column("server_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("empty_since", sa.DateTime(), nullable=True),
        sa.Column("restart_after_update", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_error", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["scheduled_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["task_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("server_id"),
    )


def downgrade():
    op.drop_table("pending_automatic_updates")
    with op.batch_alter_table("plugin_monitoring_settings") as batch:
        batch.drop_column("download_rename")
        batch.alter_column("download_url", new_column_name="link_pattern")
