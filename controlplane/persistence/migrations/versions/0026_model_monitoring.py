"""Immutable managed monitoring jobs and append-only model-quality reports."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "job_definitions",
        sa.Column("monitoring_spec", JSONB(), nullable=False, server_default="{}"),
    )
    columns = [sa.Column("id", sa.Uuid(), primary_key=True)]
    for name, target, optional in [
        ("project_id", "projects", False),
        ("job_definition_id", "job_definitions", False),
        ("model_version_id", "model_versions", False),
        ("reference_dataset_id", "dataset_versions", False),
        ("observed_dataset_id", "dataset_versions", False),
        ("feedback_dataset_id", "dataset_versions", True),
        ("job_run_id", "runs", True),
        ("pipeline_run_id", "pipeline_runs", True),
    ]:
        columns.append(
            sa.Column(
                name,
                sa.Uuid(),
                sa.ForeignKey(target + ".id", ondelete="RESTRICT"),
                nullable=optional,
            )
        )
    op.create_table(
        "monitoring_reports",
        *columns,
        sa.Column("model_name", sa.String(40), nullable=False),
        sa.Column("model_version", sa.Integer(), nullable=False),
        sa.Column("check_name", sa.String(40), nullable=False),
        sa.Column("step", sa.String(63), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_run_id", "step", name="uq_monitoring_job_occurrence"),
        sa.UniqueConstraint("pipeline_run_id", "step", name="uq_monitoring_pipeline_occurrence"),
        sa.CheckConstraint(
            "(job_run_id IS NULL) <> (pipeline_run_id IS NULL)", name="ck_monitoring_one_producer"
        ),
    )
    for column in [
        "project_id",
        "job_definition_id",
        "model_version_id",
        "reference_dataset_id",
        "observed_dataset_id",
    ]:
        op.create_index("ix_monitoring_reports_" + column, "monitoring_reports", [column])
    op.create_index(
        "ix_monitoring_project_created", "monitoring_reports", ["project_id", "created_at"]
    )
    op.create_index("ix_monitoring_model_version", "monitoring_reports", ["model_version_id"])
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'mlp_api') THEN
        GRANT SELECT ON monitoring_reports TO mlp_api;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'mlp_reconciler') THEN
        GRANT SELECT, INSERT ON monitoring_reports TO mlp_reconciler;
      END IF;
    END $$""")


def downgrade() -> None:
    op.drop_table("monitoring_reports")
    op.drop_column("job_definitions", "monitoring_spec")
