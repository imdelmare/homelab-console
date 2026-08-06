"""incident root cause correlation

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-13 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("incidents") as batch_op:
        batch_op.add_column(sa.Column("root_cause_incident_id", sa.String(length=36), nullable=True))
        batch_op.create_index(
            op.f("ix_incidents_root_cause_incident_id"), ["root_cause_incident_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_incidents_root_cause_incident_id_incidents",
            "incidents",
            ["root_cause_incident_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("incidents") as batch_op:
        batch_op.drop_constraint("fk_incidents_root_cause_incident_id_incidents", type_="foreignkey")
        batch_op.drop_index(op.f("ix_incidents_root_cause_incident_id"))
        batch_op.drop_column("root_cause_incident_id")
