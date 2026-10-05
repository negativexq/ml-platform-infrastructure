"""public endpoints: endpoint kind, protocol, exposure and limits; api keys

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_ENDPOINT_COLUMNS: tuple[sa.Column[Any], ...] = (
    sa.Column("kind", sa.String(length=16), nullable=False, server_default="model"),
    sa.Column("protocol", sa.String(length=16), nullable=False, server_default="v2-infer"),
    sa.Column("exposure", sa.String(length=16), nullable=False, server_default="internal"),
    sa.Column("limit_units_per_minute", sa.Integer(), nullable=False, server_default="600"),
    sa.Column("limit_max_body_kb", sa.Integer(), nullable=False, server_default="256"),
    sa.Column("limit_timeout_seconds", sa.Integer(), nullable=False, server_default="30"),
)


def upgrade() -> None:
    for column in _ENDPOINT_COLUMNS:
        op.add_column("endpoints", column)
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key_id", sa.String(length=8), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("endpoints", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("units_per_minute", sa.Integer(), nullable=True),
        sa.Column("secret_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_id"),
        sa.UniqueConstraint("project_id", "name", name="uq_api_keys_name"),
    )
    op.create_index(op.f("ix_api_keys_project_id"), "api_keys", ["project_id"])


def downgrade() -> None:
    op.drop_table("api_keys")
    for column in reversed(_ENDPOINT_COLUMNS):
        op.drop_column("endpoints", column.name)
