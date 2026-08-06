"""cross-watcher notification aggregation

Revision ID: 0014
Revises: 0013
"""

from alembic import op
import sqlalchemy as sa


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_outbox",
        sa.Column("group_key", sa.String(length=192), nullable=False, server_default=""),
    )
    op.add_column(
        "notification_outbox",
        sa.Column("group_items", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.create_index(
        "ix_notification_outbox_group_key", "notification_outbox", ["group_key"]
    )


def downgrade() -> None:
    op.drop_index("ix_notification_outbox_group_key", table_name="notification_outbox")
    op.drop_column("notification_outbox", "group_items")
    op.drop_column("notification_outbox", "group_key")
