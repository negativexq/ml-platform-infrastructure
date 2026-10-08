"""Isolated PostgreSQL grant/trigger tests; no Kubernetes or Docker."""

from uuid import uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from controlplane.persistence.migrate import downgrade, upgrade
from controlplane.persistence.readiness import _runtime_role
from controlplane.persistence.security import configure_roles


def test_sql_runtime_privileges_and_audit_trigger(pg_engine: Engine) -> None:
    upgrade(pg_engine)
    suffix = uuid4().hex[:12]
    users = {
        component: f"test_{component}_{suffix}" for component in ("api", "gateway", "reconciler")
    }
    with pg_engine.begin() as conn:
        for user in users.values():
            conn.execute(text(f'CREATE ROLE "{user}" LOGIN'))
    try:
        configure_roles(pg_engine, users)
        with pg_engine.begin() as conn:
            conn.execute(
                text(f'GRANT UPDATE (secret_hash) ON public.api_keys TO "{users["gateway"]}"')
            )
        with pytest.raises(RuntimeError, match="caller authorization"), pg_engine.begin() as conn:
            conn.execute(text(f'SET LOCAL SESSION AUTHORIZATION "{users["gateway"]}"'))
            _runtime_role(conn, "gateway")
        configure_roles(pg_engine, users)
        for component, user in users.items():
            with pg_engine.begin() as conn:
                conn.execute(text(f'SET LOCAL SESSION AUTHORIZATION "{user}"'))
                _runtime_role(conn, component)
                conn.execute(text("SELECT version_num FROM public.alembic_version"))
                if component in {"api", "reconciler"}:
                    conn.execute(text("SELECT * FROM public.schedule_executions LIMIT 1"))
                    conn.execute(text("UPDATE public.schedules SET paused=true WHERE false"))
                if component == "reconciler":
                    conn.execute(
                        text("UPDATE public.schedule_executions SET reason='test' WHERE false")
                    )
        with pg_engine.begin() as conn:
            conn.execute(text(f'SET LOCAL SESSION AUTHORIZATION "{users["gateway"]}"'))
            conn.execute(text("SELECT * FROM public.endpoints LIMIT 1"))
            conn.execute(text("SELECT * FROM public.memberships LIMIT 1"))
            conn.execute(text("UPDATE public.api_keys SET last_used_at=now() WHERE false"))
            conn.execute(text("INSERT INTO public.gateway_rate_buckets VALUES ('test', 5, 0)"))
            conn.execute(text("UPDATE public.gateway_rate_buckets SET tokens=4 WHERE name='test'"))
        denied = {
            "gateway": (
                "SELECT * FROM public.monitoring_reports",
                "SELECT * FROM public.data_connections",
                "SELECT * FROM public.dataset_versions",
                "SELECT * FROM public.schedules",
                "SELECT * FROM public.schedule_executions",
                "SELECT * FROM public.audit_events",
                "UPDATE public.memberships SET role='admin' WHERE false",
                "DELETE FROM public.memberships",
                "SELECT * FROM public.job_definitions",
                "UPDATE public.api_keys SET revoked_at=NULL WHERE false",
                "UPDATE public.api_keys SET endpoints='[]' WHERE false",
                "UPDATE public.api_keys SET secret_hash='x' WHERE false",
                "DELETE FROM public.gateway_rate_buckets",
            ),
            "api": (
                "INSERT INTO public.monitoring_reports SELECT * FROM public.monitoring_reports WHERE false",
                "UPDATE public.monitoring_reports SET status='STABLE' WHERE false",
                "DELETE FROM public.monitoring_reports WHERE false",
                "UPDATE public.data_connections SET name='changed' WHERE false",
                "DELETE FROM public.dataset_versions WHERE false",
                "UPDATE public.dataset_versions SET version=99 WHERE false",
                "SELECT * FROM public.gateway_rate_buckets",
                "DELETE FROM public.job_definitions",
                "UPDATE public.job_definitions SET image='tag:mutable' WHERE false",
            ),
            "reconciler": (
                "UPDATE public.monitoring_reports SET status='STABLE' WHERE false",
                "DELETE FROM public.monitoring_reports WHERE false",
                "UPDATE public.data_connections SET name='changed' WHERE false",
                "DELETE FROM public.dataset_versions WHERE false",
                "UPDATE public.dataset_versions SET version=99 WHERE false",
                "UPDATE public.api_keys SET revoked_at=NULL WHERE false",
                "SELECT * FROM public.api_keys",
                "SELECT * FROM public.memberships",
            ),
        }
        for component, user in users.items():
            for sql in denied[component] + (
                "CREATE TABLE public.escape (id int)",
                "ALTER TABLE public.projects ADD COLUMN escape int",
                "UPDATE public.audit_events SET actor='attacker'",
                "DELETE FROM public.audit_events",
                "TRUNCATE public.audit_events",
                "ALTER TABLE public.audit_events DISABLE TRIGGER mlp_audit_append_only",
            ):
                with pytest.raises(DBAPIError), pg_engine.begin() as conn:
                    conn.execute(text(f'SET LOCAL SESSION AUTHORIZATION "{user}"'))
                    conn.execute(text(sql))
        # Even the trusted migration owner must explicitly disable the trigger to mutate audit rows.
        for sql in (
            "UPDATE public.audit_events SET actor='attacker'",
            "DELETE FROM public.audit_events",
            "TRUNCATE public.audit_events",
        ):
            with pytest.raises(DBAPIError, match="append-only"), pg_engine.begin() as conn:
                conn.execute(text(sql))
        with pg_engine.begin() as conn:
            conn.execute(text(f'SET LOCAL SESSION AUTHORIZATION "{users["api"]}"'))
            conn.execute(
                text("""
                INSERT INTO public.audit_events
                (id,occurred_at,actor,action,entity_type,entity_id,project_id,payload)
                VALUES (:id,now(),'alice','test','project',:entity_id,:project_id,'{}')
            """),
                {"id": uuid4(), "entity_id": uuid4(), "project_id": uuid4()},
            )
        _core_lifecycle_with_runtime_logins(pg_engine, users)
        # Grant bootstrap rejects mixed users and owners, and does not silently widen roles.
        with pytest.raises(ValueError, match="distinct"):
            configure_roles(pg_engine, {component: users["api"] for component in users})
        # Trigger removal must precede schema downgrade.
        downgrade(pg_engine, "0019")
        upgrade(pg_engine)
    finally:
        with pg_engine.begin() as conn:
            for user in users.values():
                conn.execute(text(f'DROP OWNED BY "{user}"'))
                conn.execute(text(f'DROP ROLE "{user}"'))


def _core_lifecycle_with_runtime_logins(engine: Engine, users: dict[str, str]) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine

    from controlplane.adapters.gateway import ServingUpstream
    from controlplane.adapters.gateway.sql_limits import SqlTokenBucketLimiter
    from controlplane.application.gateway import GatewayService
    from controlplane.domain.access import Membership, ProjectRole
    from controlplane.gateway import create_gateway
    from controlplane.persistence.sql import SqlUnitOfWork, sql_uow_factory
    from controlplane.reconciliation.deployments import DeploymentReconciler
    from controlplane.tests.conftest import FakeClock
    from controlplane.tests.test_gateway import URL, Env, StubAuthenticator

    engines = {
        component: create_engine(engine.url.set(username=user)) for component, user in users.items()
    }
    try:
        api_sessions = sql_uow_factory(engines["api"])
        reconciler_sessions = sql_uow_factory(engines["reconciler"])
        gateway_sessions = sql_uow_factory(engines["gateway"])

        def api() -> SqlUnitOfWork:
            return SqlUnitOfWork(api_sessions)

        def reconciler() -> SqlUnitOfWork:
            return SqlUnitOfWork(reconciler_sessions)

        def gateway() -> SqlUnitOfWork:
            return SqlUnitOfWork(gateway_sessions)

        env = Env(api, FakeClock())
        env.expose()
        token = env.key()
        env.serving.delete(env.ref())
        recovery = DeploymentReconciler(reconciler, env.serving, env.clock)
        recovery.reconcile(env.id())
        recovery.reconcile(env.id())
        service = GatewayService(
            gateway,
            ServingUpstream(env.serving),
            SqlTokenBucketLimiter(engines["gateway"]),
            authenticator=StubAuthenticator(),
            clock=env.clock,
        )
        with TestClient(create_gateway(service)) as client:
            response = client.post(
                URL, json={"instances": [[1, 2]]}, headers={"authorization": f"Bearer {token}"}
            )
            assert response.status_code == 200, response.text
            denied = client.post(
                URL,
                json={"instances": [[1, 2]]},
                headers={"authorization": "Bearer oidc-token-for-dana"},
            )
            assert denied.status_code == 403, denied.text
            with api() as uow:
                uow.memberships.add(
                    Membership.create(
                        project_id=env.project_id,
                        subject="user:dana",
                        role=ProjectRole.INVOKER,
                        now=env.clock(),
                    )
                )
                uow.commit()
            allowed = client.post(
                URL,
                json={"instances": [[1, 2]]},
                headers={"authorization": "Bearer oidc-token-for-dana"},
            )
            assert allowed.status_code == 200, allowed.text
    finally:
        for runtime_engine in engines.values():
            runtime_engine.dispose()
