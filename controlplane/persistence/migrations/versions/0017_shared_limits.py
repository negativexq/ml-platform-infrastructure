"""Durable shared gateway rate-limit buckets."""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gateway_rate_buckets",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("tokens", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("gateway_rate_buckets")
