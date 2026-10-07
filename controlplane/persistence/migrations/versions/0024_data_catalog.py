"""Immutable project-scoped S3 connections and dataset versions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_connections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("spec", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "name", name="uq_data_connection_name"),
        sa.UniqueConstraint("project_id", "id", name="uq_data_connection_project_identity"),
    )
    op.create_index("ix_data_connections_project_id", "data_connections", ["project_id"])
    op.create_table(
        "dataset_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec", JSONB(), nullable=False),
        sa.Column("producer_run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="RESTRICT")),
        sa.Column(
            "producer_pipeline_run_id",
            sa.Uuid(),
            sa.ForeignKey("pipeline_runs.id", ondelete="RESTRICT"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "connection_id"],
            ["data_connections.project_id", "data_connections.id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("project_id", "name", "version", name="uq_dataset_version"),
        sa.CheckConstraint("version > 0", name="ck_dataset_version_positive"),
        sa.CheckConstraint(
            "producer_run_id IS NULL OR producer_pipeline_run_id IS NULL",
            name="ck_dataset_one_producer",
        ),
    )
    for column in ["project_id", "connection_id"]:
        op.create_index("ix_dataset_versions_" + column, "dataset_versions", [column])
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'mlp_api') THEN
        GRANT SELECT, INSERT ON data_connections, dataset_versions TO mlp_api;
      END IF;
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'mlp_reconciler') THEN
        GRANT SELECT, INSERT ON data_connections, dataset_versions TO mlp_reconciler;
      END IF;
    END $$""")


def downgrade() -> None:
    op.drop_table("dataset_versions")
    op.drop_table("data_connections")
