"""Add durable remediation worker jobs

Revision ID: 0021
Revises: 0020
"""

from alembic import op
import sqlalchemy as sa


revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_worker_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("client_id", sa.String(length=36), nullable=False),
        sa.Column("principal_id", sa.String(length=80), nullable=False),
        sa.Column("task_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_token_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_max_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("assigned_by", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("assignment_kind", sa.String(length=32), nullable=False, server_default="operator"),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["client_id"], ["mcp_clients.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_worker_jobs_task_id", "task_worker_jobs", ["task_id"])
    op.create_index("ix_task_worker_jobs_client_id", "task_worker_jobs", ["client_id"])
    op.create_index("ix_task_worker_jobs_principal_id", "task_worker_jobs", ["principal_id"])
    op.create_index("ix_task_worker_jobs_status", "task_worker_jobs", ["status"])
    op.create_index(
        "ux_task_worker_jobs_active_task", "task_worker_jobs", ["task_id"], unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    op.create_table(
        "task_worker_idempotency",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("lease_generation", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=36), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("result", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["task_worker_jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "lease_generation", "operation", "idempotency_key", name="uq_task_worker_idempotency_operation"),
    )
    op.create_index("ix_task_worker_idempotency_job_id", "task_worker_idempotency", ["job_id"])
    op.create_index(
        "ix_task_worker_jobs_client_status_available", "task_worker_jobs",
        ["client_id", "status", "available_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_worker_idempotency_job_id", table_name="task_worker_idempotency")
    op.drop_table("task_worker_idempotency")
    op.drop_index("ix_task_worker_jobs_client_status_available", table_name="task_worker_jobs")
    op.drop_index("ux_task_worker_jobs_active_task", table_name="task_worker_jobs")
    op.drop_index("ix_task_worker_jobs_status", table_name="task_worker_jobs")
    op.drop_index("ix_task_worker_jobs_principal_id", table_name="task_worker_jobs")
    op.drop_index("ix_task_worker_jobs_client_id", table_name="task_worker_jobs")
    op.drop_index("ix_task_worker_jobs_task_id", table_name="task_worker_jobs")
    op.drop_table("task_worker_jobs")
