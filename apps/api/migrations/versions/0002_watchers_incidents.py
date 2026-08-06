"""watchers and incidents

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-13 01:40:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "watcher_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("watcher_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_tasks", sa.Integer(), nullable=False),
        sa.Column("updated_incidents", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_watcher_runs_started_at"), "watcher_runs", ["started_at"], unique=False)
    op.create_index(op.f("ix_watcher_runs_status"), "watcher_runs", ["status"], unique=False)
    op.create_index(op.f("ix_watcher_runs_watcher_id"), "watcher_runs", ["watcher_id"], unique=False)

    op.create_table(
        "incidents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("dedupe_key", sa.String(length=128), nullable=False),
        sa.Column("watcher_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_incidents_dedupe_key"), "incidents", ["dedupe_key"], unique=False)
    op.create_index(op.f("ix_incidents_last_seen_at"), "incidents", ["last_seen_at"], unique=False)
    op.create_index(op.f("ix_incidents_severity"), "incidents", ["severity"], unique=False)
    op.create_index(op.f("ix_incidents_status"), "incidents", ["status"], unique=False)
    op.create_index(op.f("ix_incidents_task_id"), "incidents", ["task_id"], unique=False)
    op.create_index(op.f("ix_incidents_watcher_id"), "incidents", ["watcher_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_incidents_watcher_id"), table_name="incidents")
    op.drop_index(op.f("ix_incidents_task_id"), table_name="incidents")
    op.drop_index(op.f("ix_incidents_status"), table_name="incidents")
    op.drop_index(op.f("ix_incidents_severity"), table_name="incidents")
    op.drop_index(op.f("ix_incidents_last_seen_at"), table_name="incidents")
    op.drop_index(op.f("ix_incidents_dedupe_key"), table_name="incidents")
    op.drop_table("incidents")
    op.drop_index(op.f("ix_watcher_runs_watcher_id"), table_name="watcher_runs")
    op.drop_index(op.f("ix_watcher_runs_status"), table_name="watcher_runs")
    op.drop_index(op.f("ix_watcher_runs_started_at"), table_name="watcher_runs")
    op.drop_table("watcher_runs")
