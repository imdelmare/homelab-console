"""remove unused artifact and legacy task fields

Revision ID: 0015
Revises: 0014
"""

from alembic import op
import sqlalchemy as sa


revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("artifacts")
    op.drop_column("tool_invocations", "result_ref")
    op.drop_column("tasks", "model_provider")


def downgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("model_provider", sa.String(length=32), nullable=False, server_default=""),
    )
    op.add_column(
        "tool_invocations",
        sa.Column("result_ref", sa.String(length=64), nullable=False, server_default=""),
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artifacts_task_id", "artifacts", ["task_id"])
