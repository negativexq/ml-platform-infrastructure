"""Explicit PostgreSQL runtime grants. Run with a DBA URL after migrations.

Existing LOGIN users and the schema/migration owner are provisioned by the operator.
This command never accepts, creates or prints passwords. Runtime users must be dedicated
non-owner accounts without other role memberships. Future tables are denied by default;
reapply this grant manifest after every migration, before deploying the runtime pods.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping

from sqlalchemy import Connection, Engine, text

from controlplane.persistence.readiness import DatabaseReadiness
from controlplane.persistence.sql import make_engine

API_TABLES = (
    "projects",
    "job_definitions",
    "runs",
    "pipeline_definitions",
    "pipeline_runs",
    "step_runs",
    "models",
    "model_versions",
    "evaluations",
    "promotions",
    "deployments",
    "deployment_revisions",
    "rollouts",
    "endpoints",
    "api_keys",
    "memberships",
    "notification_reads",
)
RECONCILER_TABLES = (
    "projects",
    "runs",
    "pipeline_runs",
    "step_runs",
    "models",
    "model_versions",
    "evaluations",
    "promotions",
    "deployments",
    "deployment_revisions",
    "rollouts",
    "endpoints",
)
GATEWAY_TABLES = (
    "projects",
    "endpoints",
    "deployments",
    "deployment_revisions",
    "rollouts",
    "models",
    "model_versions",
    "api_keys",
    "memberships",  # OIDC invoker authorization needs read-only membership lookup.
)
ROLES = {component: f"mlp_{component}" for component in ("api", "gateway", "reconciler")}


def _ident(conn: Connection, name: str) -> str:
    return conn.dialect.identifier_preparer.quote_identifier(name)


def _grant(conn: Connection, privileges: str, tables: tuple[str, ...], role: str) -> None:
    for table in tables:
        conn.execute(
            text(
                f"GRANT {privileges} ON TABLE public.{_ident(conn, table)} TO {_ident(conn, role)}"
            )
        )


def configure_roles(engine: Engine, users: Mapping[str, str]) -> None:
    if engine.dialect.name != "postgresql":
        raise ValueError("runtime privilege separation requires PostgreSQL")
    if set(users) != set(ROLES) or len(set(users.values())) != 3:
        raise ValueError("three distinct runtime LOGIN users are required")
    if set(users.values()) & set(ROLES.values()):
        raise ValueError("LOGIN users must differ from the NOLOGIN privilege roles")
    DatabaseReadiness(engine)()
    with engine.begin() as conn:
        for component, user in users.items():
            row = conn.execute(
                text(
                    "SELECT oid, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
                    "rolreplication, rolbypassrls "
                    "FROM pg_roles WHERE rolname=:user"
                ),
                {"user": user},
            ).first()
            if row is None or not row.rolcanlogin or any(row[2:]):
                raise ValueError(f"{component} requires an existing unprivileged LOGIN user")
            owns = conn.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_class WHERE relowner=:oid) OR "
                    "EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner=:oid) OR "
                    "EXISTS (SELECT 1 FROM pg_database WHERE datdba=:oid) OR "
                    "EXISTS (SELECT 1 FROM pg_proc WHERE proowner=:oid)"
                ),
                {"oid": row.oid},
            )
            if owns:
                raise ValueError(f"{component} runtime user must not own database objects")
            memberships: set[str] = set(
                conn.execute(
                    text(
                        "SELECT parent.rolname FROM pg_auth_members m "
                        "JOIN pg_roles parent ON parent.oid=m.roleid "
                        "WHERE m.member=:oid"
                    ),
                    {"oid": row.oid},
                ).scalars()
            )
            if memberships - {ROLES[component]}:
                raise ValueError(f"{component} runtime user has unexpected role memberships")
        conn.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
        conn.execute(text("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC"))
        for component, role in ROLES.items():
            exists = conn.scalar(text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role})
            quoted = _ident(conn, role)
            if not exists:
                conn.execute(text(f"CREATE ROLE {quoted} NOLOGIN"))
            # Refuse to reuse a privileged role, rather than silently change external roles.
            unsafe = conn.scalar(
                text(
                    "SELECT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole "
                    "OR rolreplication OR rolbypassrls "
                    "OR EXISTS (SELECT 1 FROM pg_auth_members WHERE member=pg_roles.oid) "
                    "OR EXISTS (SELECT 1 FROM pg_class WHERE relowner=pg_roles.oid) "
                    "OR EXISTS (SELECT 1 FROM pg_namespace WHERE nspowner=pg_roles.oid) "
                    "OR EXISTS (SELECT 1 FROM pg_database WHERE datdba=pg_roles.oid) "
                    "OR EXISTS (SELECT 1 FROM pg_proc WHERE proowner=pg_roles.oid) "
                    "FROM pg_roles WHERE rolname=:role"
                ),
                {"role": role},
            )
            if unsafe:
                raise ValueError(f"{role} must be a dedicated unprivileged NOLOGIN role")
            for grantee in (role, users[component]):
                q = _ident(conn, grantee)
                conn.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {q}"))
                # Table REVOKE does not clear old column-level grants.
                for table in API_TABLES + (
                    "audit_events",
                    "gateway_rate_buckets",
                    "alembic_version",
                ):
                    columns: list[str] = list(
                        conn.execute(
                            text(
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_schema='public' AND table_name=:table"
                            ),
                            {"table": table},
                        ).scalars()
                    )
                    if columns:
                        cols = ", ".join(_ident(conn, c) for c in columns)
                        conn.execute(
                            text(
                                f"REVOKE SELECT ({cols}), INSERT ({cols}), UPDATE ({cols}), "
                                f"REFERENCES ({cols}) ON TABLE public.{_ident(conn, table)} "
                                f"FROM {q}"
                            )
                        )
                conn.execute(text(f"REVOKE ALL ON SCHEMA public FROM {q}"))
                conn.execute(text(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM {q}"))
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {quoted}"))
            _grant(conn, "SELECT", ("alembic_version",), role)
        _grant(conn, "SELECT, INSERT", API_TABLES, ROLES["api"])
        mutable = tuple(
            table
            for table in API_TABLES
            if table
            not in {
                "job_definitions",
                "pipeline_definitions",
                "deployment_revisions",
                "promotions",
                "notification_reads",
            }
        )
        _grant(conn, "UPDATE", mutable, ROLES["api"])
        _grant(conn, "DELETE", ("memberships",), ROLES["api"])
        _grant(conn, "SELECT, INSERT", ("audit_events",), ROLES["api"])
        _grant(
            conn,
            "SELECT",
            tuple(
                table
                for table in API_TABLES
                if table not in {"api_keys", "memberships", "notification_reads"}
            )
            + ("audit_events",),
            ROLES["reconciler"],
        )
        _grant(conn, "INSERT, UPDATE", RECONCILER_TABLES, ROLES["reconciler"])
        _grant(conn, "INSERT", ("audit_events",), ROLES["reconciler"])
        _grant(conn, "SELECT", GATEWAY_TABLES, ROLES["gateway"])
        _grant(conn, "SELECT, INSERT, UPDATE", ("gateway_rate_buckets",), ROLES["gateway"])
        conn.execute(text("GRANT UPDATE (last_used_at) ON public.api_keys TO mlp_gateway"))
        for component, role in ROLES.items():
            conn.execute(text(f"GRANT {_ident(conn, role)} TO {_ident(conn, users[component])}"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for component in ROLES:
        parser.add_argument(f"--{component}-user", required=True)
    args = parser.parse_args()
    url = os.environ.get("CP_DATABASE_ADMIN_URL")
    if not url:
        parser.error("set CP_DATABASE_ADMIN_URL to the dedicated database DBA URL")
    engine = make_engine(url)
    try:
        configure_roles(
            engine, {component: getattr(args, component + "_user") for component in ROLES}
        )
    finally:
        engine.dispose()
    print("Applied runtime privilege manifest; no credentials were printed.")


if __name__ == "__main__":
    main()
