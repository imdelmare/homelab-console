"""Durable asynchronous task router queue

Revision ID: 0018
Revises: 0017
"""

from alembic import op
import sqlalchemy as sa


revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_router_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("task_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("source", sa.String(length=32), server_default="", nullable=False),
        sa.Column("actor_kind", sa.String(length=16), server_default="system", nullable=False),
        sa.Column("actor_id", sa.String(length=80), server_default="", nullable=False),
        sa.Column("actor_label", sa.String(length=128), server_default="", nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
        sa.Column("policy_context", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), server_default="", nullable=False),
        sa.Column("error_message", sa.Text(), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_router_jobs_task_id", "task_router_jobs", ["task_id"], unique=True)
    op.create_index("ix_task_router_jobs_status", "task_router_jobs", ["status"])
    op.create_index(
        "ix_task_router_jobs_status_available",
        "task_router_jobs",
        ["status", "available_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_router_jobs_status_available", table_name="task_router_jobs")
    op.drop_index("ix_task_router_jobs_status", table_name="task_router_jobs")
    op.drop_index("ix_task_router_jobs_task_id", table_name="task_router_jobs")
    op.drop_table("task_router_jobs")
