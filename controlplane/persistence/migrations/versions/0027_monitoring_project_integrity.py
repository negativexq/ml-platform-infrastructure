"""Enforce monitoring report ownership across every parent in PostgreSQL."""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

PROJECT_PARENTS = ("job_definitions", "dataset_versions", "runs", "pipeline_runs", "models")
REPORT_PARENTS = {
    "job_definition_id": "job_definitions",
    "reference_dataset_id": "dataset_versions",
    "observed_dataset_id": "dataset_versions",
    "feedback_dataset_id": "dataset_versions",
    "job_run_id": "runs",
    "pipeline_run_id": "pipeline_runs",
    "model_id": "models",
}


def upgrade() -> None:
    for parent in PROJECT_PARENTS:
        op.create_unique_constraint(f"uq_{parent}_id_project", parent, ["id", "project_id"])
    op.create_unique_constraint("uq_model_versions_id_model", "model_versions", ["id", "model_id"])
    op.add_column("monitoring_reports", sa.Column("model_id", sa.Uuid(), nullable=True))
    op.execute(
        "UPDATE monitoring_reports r SET model_id = v.model_id "
        "FROM model_versions v WHERE r.model_version_id = v.id"
    )
    op.alter_column("monitoring_reports", "model_id", nullable=False)
    for column, parent in REPORT_PARENTS.items():
        op.create_foreign_key(
            f"fk_monitoring_{column}_project",
            "monitoring_reports",
            parent,
            [column, "project_id"],
            ["id", "project_id"],
            ondelete="RESTRICT",
        )
    # model_versions derives project ownership through its model. Both links are
    # constrained, so a report cannot fake a matching model_id from another model.
    op.create_foreign_key(
        "fk_monitoring_version_model",
        "monitoring_reports",
        "model_versions",
        ["model_version_id", "model_id"],
        ["id", "model_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_monitoring_version_model", "monitoring_reports", type_="foreignkey")
    for column in REPORT_PARENTS:
        op.drop_constraint(
            f"fk_monitoring_{column}_project", "monitoring_reports", type_="foreignkey"
        )
    op.drop_column("monitoring_reports", "model_id")
    op.drop_constraint("uq_model_versions_id_model", "model_versions", type_="unique")
    for parent in PROJECT_PARENTS:
        op.drop_constraint(f"uq_{parent}_id_project", parent, type_="unique")
