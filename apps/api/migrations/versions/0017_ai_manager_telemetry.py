"""AI manager operational telemetry

Revision ID: 0017
Revises: 0016
"""

from alembic import op
import sqlalchemy as sa


revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_usage_events", sa.Column("provider", sa.String(length=32), server_default="", nullable=False))
    op.add_column(
        "llm_usage_events",
        sa.Column("fallback_used", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "llm_usage_events",
        sa.Column("fallback_reason", sa.String(length=64), server_default="", nullable=False),
    )
    op.add_column(
        "llm_usage_events",
        sa.Column("error_kind", sa.String(length=32), server_default="", nullable=False),
    )
    op.add_column("llm_usage_events", sa.Column("queue_wait_ms", sa.Integer(), nullable=True))
    op.add_column("llm_usage_events", sa.Column("inference_latency_ms", sa.Integer(), nullable=True))
    op.add_column(
        "llm_usage_events",
        sa.Column("prompt_version", sa.String(length=64), server_default="", nullable=False),
    )
    op.add_column(
        "llm_usage_events",
        sa.Column("schema_version", sa.String(length=64), server_default="", nullable=False),
    )
    op.add_column(
        "llm_usage_events",
        sa.Column("model_version", sa.String(length=128), server_default="", nullable=False),
    )
    op.create_index("ix_llm_usage_events_provider", "llm_usage_events", ["provider"])
    op.create_index("ix_llm_usage_events_fallback_used", "llm_usage_events", ["fallback_used"])
    op.create_index("ix_llm_usage_events_error_kind", "llm_usage_events", ["error_kind"])


def downgrade() -> None:
    op.drop_index("ix_llm_usage_events_error_kind", table_name="llm_usage_events")
    op.drop_index("ix_llm_usage_events_fallback_used", table_name="llm_usage_events")
    op.drop_index("ix_llm_usage_events_provider", table_name="llm_usage_events")
    op.drop_column("llm_usage_events", "model_version")
    op.drop_column("llm_usage_events", "schema_version")
    op.drop_column("llm_usage_events", "prompt_version")
    op.drop_column("llm_usage_events", "inference_latency_ms")
    op.drop_column("llm_usage_events", "queue_wait_ms")
    op.drop_column("llm_usage_events", "error_kind")
    op.drop_column("llm_usage_events", "fallback_reason")
    op.drop_column("llm_usage_events", "fallback_used")
    op.drop_column("llm_usage_events", "provider")
