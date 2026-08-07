"""watcher dedupe and flap guard

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-13 03:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("incidents", sa.Column("missing_runs", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("incidents", sa.Column("last_missing_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_index(op.f("ix_incidents_dedupe_key"), table_name="incidents")
    op.create_index(
        "ix_incidents_dedupe_key_open",
        "incidents",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("ix_incidents_dedupe_key_open", table_name="incidents")
    op.create_index(op.f("ix_incidents_dedupe_key"), "incidents", ["dedupe_key"], unique=False)
    op.drop_column("incidents", "last_missing_at")
    op.drop_column("incidents", "missing_runs")
