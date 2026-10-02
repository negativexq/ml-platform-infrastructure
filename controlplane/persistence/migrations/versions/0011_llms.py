"""llms: model kind and serving settings, hub-sourced versions, revision serving, GPU quota

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_COLUMNS: tuple[tuple[str, sa.Column[Any]], ...] = (
    ("projects", sa.Column("gpu_quota", sa.Integer(), nullable=False, server_default="0")),
    ("models", sa.Column("kind", sa.String(length=16), nullable=False, server_default="classic")),
    ("models", sa.Column("llm_gpus", sa.Integer(), nullable=True)),
    ("models", sa.Column("llm_context_length", sa.Integer(), nullable=True)),
    ("model_versions", sa.Column("source_uri", sa.Text(), nullable=True)),
    (
        "model_versions",
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    ),
    (
        "deployment_revisions",
        sa.Column("runtime", sa.String(length=16), nullable=False, server_default="mlflow"),
    ),
    ("deployment_revisions", sa.Column("gpus", sa.Integer(), nullable=False, server_default="0")),
    ("deployment_revisions", sa.Column("context_length", sa.Integer(), nullable=True)),
)


def upgrade() -> None:
    for table, column in _COLUMNS:
        op.add_column(table, column)


def downgrade() -> None:
    for table, column in reversed(_COLUMNS):
        op.drop_column(table, column.name)
