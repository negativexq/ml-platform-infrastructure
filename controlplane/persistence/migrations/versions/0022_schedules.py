"""Durable schedules and occurrence intent, separate from executable platform runs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schedules",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("spec", JSONB(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "name", name="uq_schedules_name"),
    )
    op.create_index("ix_schedules_project_id", "schedules", ["project_id"])
    op.create_index("ix_schedules_due", "schedules", ["paused", "next_run_at"])
    op.create_table(
        "schedule_executions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "schedule_id",
            sa.Uuid(),
            sa.ForeignKey("schedules.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("scheduled_for_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_definition_id", sa.Uuid()),
        sa.Column("spec", JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "pipeline_run_id", sa.Uuid(), sa.ForeignKey("pipeline_runs.id", ondelete="RESTRICT")
        ),
        sa.Column("job_run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="RESTRICT")),
        sa.Column("reason", sa.Text()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("schedule_id", "scheduled_for_utc", name="uq_schedule_occurrence"),
        sa.UniqueConstraint("pipeline_run_id", name="uq_schedule_pipeline_run"),
        sa.UniqueConstraint("job_run_id", name="uq_schedule_job_run"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'DISPATCHED', 'SKIPPED', 'MISSED')",
            name="ck_schedule_execution_status",
        ),
        sa.CheckConstraint(
            "(status = 'DISPATCHED' AND resolved_definition_id IS NOT NULL AND "
            "((pipeline_run_id IS NOT NULL AND job_run_id IS NULL) OR "
            "(pipeline_run_id IS NULL AND job_run_id IS NOT NULL))) OR "
            "(status <> 'DISPATCHED' AND pipeline_run_id IS NULL AND job_run_id IS NULL)",
            name="ck_schedule_execution_run",
        ),
    )
    for column in ["schedule_id", "project_id", "pipeline_run_id", "job_run_id"]:
        op.create_index("ix_schedule_executions_" + column, "schedule_executions", [column])
    op.create_index(
        "ix_schedule_executions_queue", "schedule_executions", ["status", "scheduled_for_utc"]
    )

    # Upgrade existing installations without requiring broad default-table grants.
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'mlp_api') THEN
        GRANT SELECT, INSERT ON schedules, schedule_executions TO mlp_api;
        GRANT UPDATE ON schedules TO mlp_api;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'mlp_reconciler') THEN
        GRANT SELECT, INSERT, UPDATE ON schedules, schedule_executions TO mlp_reconciler;
      END IF;
    END $$""")


def downgrade() -> None:
    op.drop_table("schedule_executions")
    op.drop_table("schedules")
