"""Add task router lease fencing token

Revision ID: 0019
Revises: 0018
"""

from alembic import op
import sqlalchemy as sa


revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_router_jobs",
        sa.Column("lease_token", sa.String(length=36), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("task_router_jobs", "lease_token")
