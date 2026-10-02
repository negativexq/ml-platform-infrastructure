"""pipeline run and step run execution state

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pipeline_runs", sa.Column("status_reason", sa.Text(), nullable=True))
    op.add_column(
        "pipeline_runs",
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("pipeline_runs", "cancel_requested", server_default=None)
    op.add_column("pipeline_runs", sa.Column("commit_sha", sa.String(length=64), nullable=True))
    op.add_column(
        "pipeline_runs", sa.Column("idempotency_key", sa.String(length=200), nullable=True)
    )
    op.add_column(
        "pipeline_runs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "pipeline_runs", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_unique_constraint(
        "uq_pipeline_runs_idempotency_key", "pipeline_runs", ["project_id", "idempotency_key"]
    )
    op.create_index("ix_pipeline_runs_status", "pipeline_runs", ["status"])

    op.add_column("step_runs", sa.Column("status_reason", sa.Text(), nullable=True))
    op.add_column("step_runs", sa.Column("exit_code", sa.Integer(), nullable=True))
    op.add_column("step_runs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("step_runs", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for column in ("finished_at", "started_at", "exit_code", "status_reason"):
        op.drop_column("step_runs", column)
    op.drop_index("ix_pipeline_runs_status", table_name="pipeline_runs")
    op.drop_constraint("uq_pipeline_runs_idempotency_key", "pipeline_runs", type_="unique")
    for column in (
        "finished_at",
        "started_at",
        "idempotency_key",
        "commit_sha",
        "cancel_requested",
        "status_reason",
    ):
        op.drop_column("pipeline_runs", column)
