"""persist the latest normalized provider error

Revision ID: 0011
Revises: 0010
"""

from alembic import op
import sqlalchemy as sa


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provider_configurations",
        sa.Column("last_error_status", sa.String(length=16), server_default="", nullable=False),
    )
    op.add_column(
        "provider_configurations",
        sa.Column("last_error_detail", sa.Text(), server_default="", nullable=False),
    )
    op.add_column(
        "provider_configurations",
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("provider_configurations", "last_error_at")
    op.drop_column("provider_configurations", "last_error_detail")
    op.drop_column("provider_configurations", "last_error_status")
