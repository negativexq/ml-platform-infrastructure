"""Snapshot LLM replica limits and reserve maximum GPU capacity."""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, prefix in (("models", "llm_"), ("deployment_revisions", "")):
        for name in ("min_scale", "max_scale"):
            op.add_column(
                table, sa.Column(prefix + name, sa.Integer(), nullable=False, server_default="1")
            )


def downgrade() -> None:
    for table, prefix in (("deployment_revisions", ""), ("models", "llm_")):
        for name in ("max_scale", "min_scale"):
            op.drop_column(table, prefix + name)
