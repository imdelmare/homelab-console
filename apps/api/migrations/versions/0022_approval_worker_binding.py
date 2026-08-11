"""Bind worker approvals to a fenced lease

Revision ID: 0022
Revises: 0021
"""

from alembic import op
import sqlalchemy as sa


revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("approvals", sa.Column("worker_job_id", sa.String(length=36), nullable=True))
    op.add_column("approvals", sa.Column("worker_lease_generation", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_approvals_worker_job_id_task_worker_jobs",
        "approvals", "task_worker_jobs", ["worker_job_id"], ["id"],
    )
    op.create_index("ix_approvals_worker_job_id", "approvals", ["worker_job_id"])


def downgrade() -> None:
    op.drop_index("ix_approvals_worker_job_id", table_name="approvals")
    op.drop_constraint("fk_approvals_worker_job_id_task_worker_jobs", "approvals", type_="foreignkey")
    op.drop_column("approvals", "worker_lease_generation")
    op.drop_column("approvals", "worker_job_id")
