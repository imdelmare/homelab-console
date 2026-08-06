"""incident resolution fields

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-13 02:20:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("watcher_runs", sa.Column("resolved_incidents", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("incidents", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("incidents", sa.Column("resolution_reason", sa.String(length=64), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("incidents", "resolution_reason")
    op.drop_column("incidents", "resolved_at")
    op.drop_column("watcher_runs", "resolved_incidents")
