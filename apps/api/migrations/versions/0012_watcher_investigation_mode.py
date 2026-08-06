"""persist watcher investigation mode

Revision ID: 0012
Revises: 0011
"""

from alembic import op
import sqlalchemy as sa


revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "watcher_configs",
        sa.Column(
            "investigation_mode",
            sa.String(length=24),
            server_default="manual",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("watcher_configs", "investigation_mode")
