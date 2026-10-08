"""Immutable managed batch specification on job definitions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_definitions", sa.Column("batch_spec", JSONB(), nullable=False, server_default="{}")
    )


def downgrade() -> None:
    op.drop_column("job_definitions", "batch_spec")
