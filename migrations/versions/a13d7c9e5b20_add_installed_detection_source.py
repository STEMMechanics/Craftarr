"""Allow plugin update checks to select installed-version input."""
from alembic import op
import sqlalchemy as sa


revision = 'a13d7c9e5b20'
down_revision = 'e2b18f467a09'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'plugin_monitoring_settings',
        sa.Column('installed_detection', sa.String(length=32), nullable=False, server_default='auto'),
    )


def downgrade():
    op.drop_column('plugin_monitoring_settings', 'installed_detection')
