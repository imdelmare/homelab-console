"""Luna usage telemetry and task router reviews

Revision ID: 0013
Revises: 0012
"""

from alembic import op
import sqlalchemy as sa


revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_usage_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("component", sa.String(length=32), nullable=False),
        sa.Column("reference_id", sa.String(length=64), nullable=True),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=False),
        sa.Column("metered", sa.Boolean(), nullable=False),
        sa.Column("input_price_per_million", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("cached_input_price_per_million", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("output_price_per_million", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("attributed_cost_usd", sa.Numeric(precision=14, scale=8), nullable=True),
        sa.Column("pricing_source", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("component", "reference_id", name="uq_llm_usage_component_reference"),
    )
    op.create_index("ix_llm_usage_events_component", "llm_usage_events", ["component"])
    op.create_index("ix_llm_usage_events_task_id", "llm_usage_events", ["task_id"])
    op.create_index("ix_llm_usage_events_model", "llm_usage_events", ["model"])
    op.create_index("ix_llm_usage_events_status", "llm_usage_events", ["status"])
    op.create_index("ix_llm_usage_events_created_at", "llm_usage_events", ["created_at"])

    op.create_table(
        "task_router_reviews",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("decision_event_id", sa.String(length=36), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("corrections", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("reviewed_by", sa.String(length=80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["decision_event_id"], ["task_events.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "decision_event_id",
            name="uq_task_router_reviews_decision_event_id",
        ),
    )
    op.create_index("ix_task_router_reviews_task_id", "task_router_reviews", ["task_id"], unique=True)
    op.create_index("ix_task_router_reviews_verdict", "task_router_reviews", ["verdict"])


def downgrade() -> None:
    op.drop_index("ix_task_router_reviews_verdict", table_name="task_router_reviews")
    op.drop_index("ix_task_router_reviews_task_id", table_name="task_router_reviews")
    op.drop_table("task_router_reviews")
    op.drop_index("ix_llm_usage_events_created_at", table_name="llm_usage_events")
    op.drop_index("ix_llm_usage_events_status", table_name="llm_usage_events")
    op.drop_index("ix_llm_usage_events_model", table_name="llm_usage_events")
    op.drop_index("ix_llm_usage_events_task_id", table_name="llm_usage_events")
    op.drop_index("ix_llm_usage_events_component", table_name="llm_usage_events")
    op.drop_table("llm_usage_events")
