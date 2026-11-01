"""Add Paper TPS and MSPT values to historical server metrics."""
from alembic import op
import sqlalchemy as sa


revision = "e7f3a9c2b1d4"
down_revision = "d4a1e5b7c9e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("server_metrics", sa.Column("tps", sa.Float(), nullable=True))
    op.add_column("server_metrics", sa.Column("mspt", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("server_metrics", "mspt")
    op.drop_column("server_metrics", "tps")
