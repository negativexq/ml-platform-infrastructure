"""Durable pipeline model-discovery progress."""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("models_discovered_at", "model_discovery_checked_at"):
        op.add_column("pipeline_runs", sa.Column(name, sa.DateTime(timezone=True)))
    op.create_index(
        "ix_pipeline_runs_discovery",
        "pipeline_runs",
        ["status", "models_discovered_at", "model_discovery_checked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_pipeline_runs_discovery", table_name="pipeline_runs")
    for name in ("model_discovery_checked_at", "models_discovered_at"):
        op.drop_column("pipeline_runs", name)
