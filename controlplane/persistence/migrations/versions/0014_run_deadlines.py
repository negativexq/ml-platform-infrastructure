"""Persist job defaults and immutable run deadlines.

Revision ID: 0014
Revises: 0013
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("job_definitions", "runs", "pipeline_runs"):
        op.add_column(
            table, sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="3600")
        )


def downgrade() -> None:
    for table in ("pipeline_runs", "runs", "job_definitions"):
        op.drop_column(table, "timeout_seconds")
