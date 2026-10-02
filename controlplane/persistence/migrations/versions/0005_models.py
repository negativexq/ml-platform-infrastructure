"""model thresholds, drift flag, version lineage, evaluation checks

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "models",
        sa.Column(
            "thresholds",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("models", "thresholds", server_default=None)
    op.add_column("models", sa.Column("alias_drift", sa.Text(), nullable=True))

    op.alter_column(
        "model_versions",
        "external_ref",
        existing_type=sa.Text(),
        type_=sa.String(length=200),
        existing_nullable=True,
    )
    op.add_column("model_versions", sa.Column("source_pipeline_run_id", sa.Uuid(), nullable=True))
    op.create_unique_constraint(
        "uq_model_versions_external_ref", "model_versions", ["model_id", "external_ref"]
    )

    op.create_index(
        "uq_model_versions_one_champion",
        "model_versions",
        ["model_id"],
        unique=True,
        postgresql_where=sa.text("status = 'CHAMPION'"),
    )

    op.add_column(
        "evaluations",
        sa.Column(
            "checks",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.alter_column("evaluations", "checks", server_default=None)


def downgrade() -> None:
    op.drop_column("evaluations", "checks")
    op.drop_index("uq_model_versions_one_champion", table_name="model_versions")
    op.drop_constraint("uq_model_versions_external_ref", "model_versions", type_="unique")
    op.drop_column("model_versions", "source_pipeline_run_id")
    op.alter_column(
        "model_versions",
        "external_ref",
        existing_type=sa.String(length=200),
        type_=sa.Text(),
        existing_nullable=True,
    )
    op.drop_column("models", "alias_drift")
    op.drop_column("models", "thresholds")
