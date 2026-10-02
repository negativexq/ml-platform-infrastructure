"""trace context: traceparent on reconciler-driven entities, trace_id on audit events

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_TABLES = ("projects", "runs", "pipeline_runs", "deployments", "rollouts")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("traceparent", sa.String(length=128), nullable=True))
    op.add_column("audit_events", sa.Column("trace_id", sa.String(length=32), nullable=True))
    op.create_index(op.f("ix_audit_events_trace_id"), "audit_events", ["trace_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_events_trace_id"), table_name="audit_events")
    op.drop_column("audit_events", "trace_id")
    for table in reversed(_TABLES):
        op.drop_column(table, "traceparent")
