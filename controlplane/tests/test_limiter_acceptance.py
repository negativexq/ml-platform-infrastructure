"""Opt-in PostgreSQL gates; never auto-start PostgreSQL."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from controlplane.adapters.gateway.sql_limits import SqlTokenBucketLimiter
from controlplane.persistence.sql import make_engine

pytestmark = pytest.mark.skipif(
    not os.environ.get("CP_ACCEPTANCE_DATABASE_URL"), reason="explicit isolated PostgreSQL required"
)


def test_sql_shared_postgres_budget_and_lock_timeout() -> None:
    engine = make_engine(os.environ["CP_ACCEPTANCE_DATABASE_URL"])
    other = make_engine(os.environ["CP_ACCEPTANCE_DATABASE_URL"])
    name = "acceptance-" + uuid4().hex
    buckets = [(name + "-endpoint", 10), (name + "-caller", 10)]
    a, b = SqlTokenBucketLimiter(engine), SqlTokenBucketLimiter(other)
    try:
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(lambda i: (a if i % 2 else b).take(buckets, 1), range(40)))
        assert sum(result.allowed for result in results) == 10
        with engine.begin() as connection:
            connection.execute(
                text("SELECT name FROM gateway_rate_buckets WHERE name=:name FOR UPDATE"),
                {"name": buckets[0][0]},
            )
            started = time.monotonic()
            with pytest.raises(DBAPIError):
                b.take(buckets, 1)
            assert 1.5 <= time.monotonic() - started < 5
        a.refund(buckets, 5)
        assert b.take(buckets, 1).allowed
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM gateway_rate_buckets WHERE name LIKE :prefix"),
                {"prefix": name + "%"},
            )
        engine.dispose()
        other.dispose()


def test_sql_migration_lock_refuses_a_second_process() -> None:
    from controlplane.persistence.migrate import _lock, upgrade

    engine = make_engine(os.environ["CP_ACCEPTANCE_DATABASE_URL"])
    other = make_engine(os.environ["CP_ACCEPTANCE_DATABASE_URL"])
    try:
        with engine.begin() as connection:
            _lock(connection)
            with pytest.raises(RuntimeError, match="owns"):
                upgrade(other)
    finally:
        engine.dispose()
        other.dispose()
