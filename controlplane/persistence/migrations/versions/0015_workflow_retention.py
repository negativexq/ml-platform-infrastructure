"""Track workflow cleanup without deleting run history.

Revision ID: 0015
Revises: 0014
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("runs", "pipeline_runs"):
        op.add_column(table, sa.Column("workflow_cleaned_at", sa.DateTime(timezone=True)))
        op.create_index(
            f"ix_{table}_retention", table, ["status", "workflow_cleaned_at", "finished_at"]
        )


def downgrade() -> None:
    for table in ("pipeline_runs", "runs"):
        op.drop_index(f"ix_{table}_retention", table_name=table)
        op.drop_column(table, "workflow_cleaned_at")
