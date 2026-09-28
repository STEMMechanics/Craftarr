"""Add an optional GitHub JAR asset filename expression."""
from alembic import op
import sqlalchemy as sa

revision = 'f03a8d8c12ab'
down_revision = '49d32eaf51c6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'plugin_monitoring_settings',
        sa.Column('asset_pattern', sa.Text(), nullable=False, server_default=''),
    )


def downgrade():
    op.drop_column('plugin_monitoring_settings', 'asset_pattern')
