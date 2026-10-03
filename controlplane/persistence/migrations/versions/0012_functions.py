"""functions: a function's scaling and environment, kept with the model and each revision

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_TABLES = ("models", "deployment_revisions")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("function_settings", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_column(table, "function_settings")
