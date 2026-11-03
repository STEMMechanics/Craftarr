"""Retain the remote destination used by scheduled backup jobs."""
from alembic import op
import sqlalchemy as sa


revision = "f4c8b9d2a1e7"
down_revision = "e7f3a9c2b1d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("backup_jobs", sa.Column("remote_destination", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("backup_jobs", "remote_destination")
