"""Track pending plugin changes to the active server process."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7dcb8a9016fe"
down_revision: Union[str, Sequence[str], None] = "f03a8d8c12ab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "servers",
        sa.Column("plugin_session_pid", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("servers", "plugin_session_pid")
