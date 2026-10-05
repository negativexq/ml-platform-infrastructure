from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from controlplane.api.app import create_app
from controlplane.api.auth import AuthConfig
from controlplane.gateway.app import create_gateway
from controlplane.persistence.memory import MemoryStore, MemoryUnitOfWork
from controlplane.persistence.readiness import DatabaseReadiness


@pytest.mark.parametrize("gateway", [False, True])
def test_dependency_failure_affects_readiness_only(gateway: bool) -> None:
    check = Mock(side_effect=RuntimeError("password=secret"))
    app = (
        create_gateway(Mock(), readiness=check)
        if gateway
        else create_app(
            lambda: MemoryUnitOfWork(MemoryStore()), readiness=check, auth=AuthConfig(Mock())
        )
    )
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        response = client.get("/readyz")
        assert response.status_code == 503 and "secret" not in response.text
        check.side_effect = None
        assert client.get("/readyz").status_code == 200


def test_database_probe_requires_exact_migration_heads() -> None:
    engine = create_engine("sqlite://")
    check = DatabaseReadiness(engine)
    with pytest.raises(OperationalError):
        check()  # no schema
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num TEXT)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('old')"))
    with pytest.raises(RuntimeError, match="migrations"):
        check()
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM alembic_version"))
        for head in check._heads:
            connection.execute(text("INSERT INTO alembic_version VALUES (:head)"), {"head": head})
    check()
    engine.dispose()
