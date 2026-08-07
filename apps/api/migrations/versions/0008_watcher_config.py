"""watcher runtime config

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-14 14:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "watcher_configs",
        sa.Column("watcher_id", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("min_severity", sa.String(length=16), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("watcher_id"),
    )


def downgrade() -> None:
    op.drop_table("watcher_configs")
