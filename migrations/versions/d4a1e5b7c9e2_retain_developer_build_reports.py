"""Retain Gradle problems reports for developer plugin builds."""
from alembic import op
import sqlalchemy as sa


revision = "d4a1e5b7c9e2"
down_revision = "c96d348a21f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("developer_plugin_updates", sa.Column("build_report_html", sa.Text(), nullable=True))
    op.add_column("developer_plugin_updates", sa.Column("build_report_path", sa.String(length=1024), nullable=True))


def downgrade() -> None:
    op.drop_column("developer_plugin_updates", "build_report_path")
    op.drop_column("developer_plugin_updates", "build_report_html")
