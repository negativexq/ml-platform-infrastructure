"""Freeze batch dataset selection in the transaction that creates each run."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("batch_snapshot", JSONB(), nullable=False, server_default="{}"))
    op.add_column(
        "pipeline_runs", sa.Column("batch_snapshots", JSONB(), nullable=False, server_default="{}")
    )
    op.execute(
        "CREATE INDEX ix_dataset_processing_date ON dataset_versions "
        "(project_id, name, (spec->>'processing_date'), version DESC)"
    )


def downgrade() -> None:
    op.drop_index("ix_dataset_processing_date", "dataset_versions")
    op.drop_column("pipeline_runs", "batch_snapshots")
    op.drop_column("runs", "batch_snapshot")
