"""Require dataset producers to belong to the dataset's project."""

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

PRODUCERS = {
    "fk_dataset_producer_run_project": ("producer_run_id", "runs"),
    "fk_dataset_producer_pipeline_project": ("producer_pipeline_run_id", "pipeline_runs"),
}


def upgrade() -> None:
    # Parent (id, project_id) uniqueness was introduced by 0027. Default
    # MATCH SIMPLE permits registered datasets with no producer. Existing
    # inconsistent links fail validation instead of being silently rewritten.
    for name, (column, parent) in PRODUCERS.items():
        op.create_foreign_key(
            name,
            "dataset_versions",
            parent,
            [column, "project_id"],
            ["id", "project_id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    for name in PRODUCERS:
        op.drop_constraint(name, "dataset_versions", type_="foreignkey")
