from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from controlplane.api.app import create_app
from controlplane.application.ports import UnitOfWork
from controlplane.persistence.memory import MemoryStore, MemoryUnitOfWork


class FakeClock:
    """Deterministic and strictly increasing."""

    def __init__(self) -> None:
        self._now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        self._now += timedelta(seconds=1)
        return self._now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# --- PostgreSQL ------------------------------------------------------------
# CP_TEST_DATABASE_URL (CI service container) wins; otherwise an embedded
# PostgreSQL from `pgserver` is started; otherwise the SQL tests are skipped.


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    explicit = os.environ.get("CP_TEST_DATABASE_URL")
    if explicit:
        yield explicit
        return
    try:
        import pgserver
    except ImportError:
        pytest.skip("no PostgreSQL: set CP_TEST_DATABASE_URL or install controlplane-dev")
    server = pgserver.get_server(  # type: ignore[attr-defined]
        tempfile.mkdtemp(prefix="cp-pg-"), cleanup_mode="stop"
    )
    yield server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    server.cleanup()


@pytest.fixture
def pg_engine(pg_url: str) -> Iterator[Engine]:
    from sqlalchemy import text

    from controlplane.persistence.sql import make_engine

    engine = make_engine(pg_url)
    with engine.begin() as conn:  # every test starts from a truly empty database
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


@pytest.fixture(params=["memory", "sql"])
def uow_factory(request: pytest.FixtureRequest) -> Callable[[], UnitOfWork]:
    """The same use-case tests run against the fake and the real database."""
    if request.param == "memory":
        store = MemoryStore()
        return lambda: MemoryUnitOfWork(store)
    from controlplane.persistence.migrate import upgrade
    from controlplane.persistence.sql import SqlUnitOfWork, sql_uow_factory

    engine = request.getfixturevalue("pg_engine")
    upgrade(engine)
    sessions = sql_uow_factory(engine)
    return lambda: SqlUnitOfWork(sessions)


@pytest.fixture
def client(uow_factory: Callable[[], UnitOfWork], clock: FakeClock) -> TestClient:
    return TestClient(create_app(uow_factory, clock))
