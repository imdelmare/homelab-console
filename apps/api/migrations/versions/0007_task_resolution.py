"""task resolution capture

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-13 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_resolutions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("incident_id", sa.String(length=36), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("resolved_findings", sa.JSON(), nullable=False),
        sa.Column("resolved_by", sa.String(length=80), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_task_resolutions_task_id"), "task_resolutions", ["task_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_task_resolutions_task_id"), table_name="task_resolutions")
    op.drop_table("task_resolutions")
