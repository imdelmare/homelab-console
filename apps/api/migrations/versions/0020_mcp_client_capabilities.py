"""Add operator-granted MCP client capabilities

Revision ID: 0020
Revises: 0019
"""

from alembic import op
import sqlalchemy as sa


revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mcp_clients",
        sa.Column(
            "capabilities",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
    )
    op.add_column(
        "mcp_clients",
        sa.Column("principal_id", sa.String(length=80), server_default="", nullable=False),
    )
    op.create_index(
        "ux_mcp_clients_principal_id_nonempty",
        "mcp_clients",
        ["principal_id"],
        unique=True,
        postgresql_where=sa.text("principal_id <> ''"),
    )


def downgrade() -> None:
    op.drop_index("ux_mcp_clients_principal_id_nonempty", table_name="mcp_clients")
    op.drop_column("mcp_clients", "principal_id")
    op.drop_column("mcp_clients", "capabilities")
