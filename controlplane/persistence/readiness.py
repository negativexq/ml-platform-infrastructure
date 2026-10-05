"""Read-only database/schema checks for production HTTP probes."""

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, text

from controlplane.persistence.migrate import SCRIPT_LOCATION


class DatabaseReadiness:
    def __init__(self, engine: Engine, *, component: str | None = None) -> None:
        self._engine = engine
        self._component = component
        config = Config()
        config.set_main_option("script_location", str(SCRIPT_LOCATION))
        self._heads = set(ScriptDirectory.from_config(config).get_heads())

    def __call__(self) -> None:
        with self._engine.connect() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL statement_timeout = '2000ms'"))
            connection.execute(text("SELECT 1"))
            if self._component is not None:
                _runtime_role(connection, self._component)
            current: set[str] = set(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
            )
            if current != self._heads:
                raise RuntimeError("database migrations do not match this release")


def _runtime_role(connection: "Connection", component: str) -> None:
    if component not in {"api", "gateway", "reconciler"}:
        raise ValueError("unknown database component")
    if connection.dialect.name != "postgresql":
        raise RuntimeError("runtime database privilege enforcement requires PostgreSQL")
    safe = connection.scalar(
        text("""
        SELECT rolcanlogin AND current_user=session_user
          AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
          AND pg_has_role(current_user, :role, 'USAGE')
          AND NOT has_schema_privilege(current_user, 'public', 'CREATE')
          AND NOT EXISTS (
            SELECT 1 FROM pg_auth_members m JOIN pg_roles p ON p.oid=m.roleid
            WHERE m.member=pg_roles.oid AND p.rolname <> :role)
          AND NOT EXISTS (SELECT 1 FROM pg_class WHERE relowner=pg_roles.oid)
          AND NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner=pg_roles.oid)
          AND NOT EXISTS (SELECT 1 FROM pg_database WHERE datdba=pg_roles.oid)
        FROM pg_roles WHERE rolname=current_user
    """),
        {"role": "mlp_" + component},
    )
    if not safe:
        raise RuntimeError("database runtime role is missing, privileged or owns schema objects")
    for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
        if connection.scalar(
            text("SELECT has_table_privilege(current_user, 'public.audit_events', :privilege)"),
            {"privilege": privilege},
        ):
            raise RuntimeError("runtime role can mutate existing audit records")
    if component == "gateway":
        for table in ("audit_events", "job_definitions"):
            if connection.scalar(
                text("SELECT has_table_privilege(current_user, :table, 'SELECT')"),
                {"table": "public." + table},
            ):
                raise RuntimeError("gateway database role has excess read permissions")
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            if connection.scalar(
                text("SELECT has_table_privilege(current_user, 'public.memberships', :privilege)"),
                {"privilege": privilege},
            ):
                raise RuntimeError("gateway database role can change project authorization")
        for column in ("secret_hash", "endpoints", "units_per_minute", "expires_at", "revoked_at"):
            if connection.scalar(
                text(
                    "SELECT has_column_privilege(current_user, 'public.api_keys', "
                    ":column, 'UPDATE')"
                ),
                {"column": column},
            ):
                raise RuntimeError("gateway database role can change caller authorization")
