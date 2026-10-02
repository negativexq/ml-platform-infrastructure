"""rollouts

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rollouts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("from_revision", sa.Integer(), nullable=False),
        sa.Column("to_revision", sa.Integer(), nullable=False),
        sa.Column("model_version_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("steps", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("current_step", sa.Integer(), nullable=False),
        sa.Column("gate", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("step_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("abort_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["deployment_id"], ["deployments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["model_version_id"], ["model_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_rollouts_deployment_id"), "rollouts", ["deployment_id"])
    op.create_index(op.f("ix_rollouts_model_version_id"), "rollouts", ["model_version_id"])
    op.create_index(
        "uq_rollouts_one_active",
        "rollouts",
        ["deployment_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'PROGRESSING')"),
    )


def downgrade() -> None:
    op.drop_table("rollouts")
