"""Schema and migration tests against a real PostgreSQL."""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, text

from controlplane.persistence.migrate import downgrade, upgrade
from controlplane.persistence.models import Base

CORE_TABLES = {
    "projects",
    "job_definitions",
    "pipeline_definitions",
    "pipeline_runs",
    "step_runs",
    "models",
    "model_versions",
    "evaluations",
    "promotions",
    "deployments",
    "deployment_revisions",
    "endpoints",
}


def test_migrations_apply_to_a_clean_database(pg_engine: Engine) -> None:
    upgrade(pg_engine)
    tables = set(inspect(pg_engine).get_table_names())
    assert CORE_TABLES | {"audit_events", "alembic_version"} <= tables


def test_every_core_entity_has_a_uuid_primary_key(pg_engine: Engine) -> None:
    upgrade(pg_engine)
    insp = inspect(pg_engine)
    for table in CORE_TABLES | {"audit_events"}:
        pk = insp.get_pk_constraint(table)["constrained_columns"]
        assert pk == ["id"], table
        column = next(c for c in insp.get_columns(table) if c["name"] == "id")
        assert str(column["type"]).upper() == "UUID", table


def test_migrations_match_the_models_exactly(pg_engine: Engine) -> None:
    """No drift between what Alembic builds and what the ORM expects."""
    upgrade(pg_engine)
    with pg_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_upgrade_is_repeatable_and_downgrade_is_clean(pg_engine: Engine) -> None:
    upgrade(pg_engine)
    upgrade(pg_engine)  # already at head: no-op
    downgrade(pg_engine, "base")
    with pg_engine.connect() as conn:
        left = conn.execute(
            text(
                "select table_name from information_schema.tables "
                "where table_schema = 'public' and table_name <> 'alembic_version'"
            )
        ).all()
    assert left == []
    upgrade(pg_engine)  # and back up again


def test_project_name_is_unique_in_the_database(pg_engine: Engine) -> None:
    upgrade(pg_engine)
    uniques = {u["name"] for u in inspect(pg_engine).get_unique_constraints("projects")}
    assert "uq_projects_name" in uniques
