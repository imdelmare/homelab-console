"""execution and watcher invariants

Revision ID: 0010
Revises: 0009
"""

from alembic import op
import sqlalchemy as sa


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ux_tool_invocations_approval_id",
        "tool_invocations",
        ["approval_id"],
        unique=True,
    )
    op.create_table(
        "watcher_automation_state",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_by", sa.String(length=80), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("watcher_automation_state")
    op.drop_index(
        "ux_tool_invocations_approval_id",
        "tool_invocations",
    )
