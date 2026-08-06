"""mcp client registrations

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-13 10:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_clients",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("client_label", sa.String(length=128), nullable=False),
        sa.Column("host_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("token_hint", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=256), nullable=False),
        sa.Column("created_by", sa.String(length=80), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_mcp_clients_agent_id"), "mcp_clients", ["agent_id"], unique=False)
    op.create_index(op.f("ix_mcp_clients_token_hash"), "mcp_clients", ["token_hash"], unique=True)
    op.create_table(
        "mcp_pairing_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column("client_label", sa.String(length=128), nullable=False),
        sa.Column("host_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("pairing_secret_hash", sa.String(length=128), nullable=False),
        sa.Column("approve_nonce_hash", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("denied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(length=80), nullable=False),
        sa.Column("delivery_status", sa.String(length=16), nullable=False),
        sa.Column("telegram_message_id", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_mcp_pairing_requests_agent_id"), "mcp_pairing_requests", ["agent_id"], unique=False)
    op.create_index(op.f("ix_mcp_pairing_requests_approve_nonce_hash"), "mcp_pairing_requests", ["approve_nonce_hash"], unique=False)
    op.create_index(op.f("ix_mcp_pairing_requests_status"), "mcp_pairing_requests", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_mcp_pairing_requests_status"), table_name="mcp_pairing_requests")
    op.drop_index(op.f("ix_mcp_pairing_requests_approve_nonce_hash"), table_name="mcp_pairing_requests")
    op.drop_index(op.f("ix_mcp_pairing_requests_agent_id"), table_name="mcp_pairing_requests")
    op.drop_table("mcp_pairing_requests")
    op.drop_index(op.f("ix_mcp_clients_token_hash"), table_name="mcp_clients")
    op.drop_index(op.f("ix_mcp_clients_agent_id"), table_name="mcp_clients")
    op.drop_table("mcp_clients")
