"""Immutable parameter schemas and resolved execution snapshots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, column in [
        ("job_definitions", "parameter_schema"),
        ("pipeline_definitions", "parameter_schema"),
        ("runs", "parameters"),
        ("pipeline_runs", "parameters"),
    ]:
        op.add_column(table, sa.Column(column, JSONB(), nullable=False, server_default="{}"))


def downgrade() -> None:
    for table, column in [
        ("job_definitions", "parameter_schema"),
        ("pipeline_definitions", "parameter_schema"),
        ("runs", "parameters"),
        ("pipeline_runs", "parameters"),
    ]:
        op.drop_column(table, column)
