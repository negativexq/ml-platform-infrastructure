"""Persist names of workload secret references, never their values."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("job_definitions", "models", "deployment_revisions"):
        op.add_column(table, sa.Column("secret_refs", JSONB(), nullable=False, server_default="{}"))


def downgrade() -> None:
    for table in ("deployment_revisions", "models", "job_definitions"):
        op.drop_column(table, "secret_refs")
