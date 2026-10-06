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


def test_sql_bucket_operations_and_clock_after_lock() -> None:
    """Exercise debt/refunds and prove the shared clock is read after a contended lock."""
    engine = make_engine(os.environ["CP_ACCEPTANCE_DATABASE_URL"])
    other = make_engine(os.environ["CP_ACCEPTANCE_DATABASE_URL"])
    prefix = "acceptance-" + uuid4().hex
    buckets = [(prefix + "-b", 600), (prefix + "-a", 600)]
    limiter = SqlTokenBucketLimiter(other)
    try:
        assert limiter.take(buckets, 600).allowed
        assert not limiter.take(buckets, 1).allowed
        limiter.refund(buckets, 25)
        assert limiter.take(buckets, 20).allowed
        limiter.charge(buckets, 50)
        assert not limiter.admit(buckets).allowed
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE gateway_rate_buckets SET tokens=0, "
                    "updated_at=extract(epoch FROM clock_timestamp()) WHERE name LIKE :prefix"
                ),
                {"prefix": prefix + "%"},
            )
            # The statement must acquire both rows in name order, regardless of caller order.
            connection.execute(
                text("SELECT name FROM gateway_rate_buckets WHERE name=:name FOR UPDATE"),
                {"name": prefix + "-a"},
            )
            with ThreadPoolExecutor(max_workers=1) as workers:
                pending = workers.submit(limiter.take, buckets, 3)
                time.sleep(0.4)
                assert not pending.done()
                released_at = float(
                    connection.scalar(text("SELECT extract(epoch FROM clock_timestamp())"))
                )
                connection.commit()
                assert pending.result(timeout=4).allowed
        with engine.connect() as connection:
            updated = (
                connection.execute(
                    text("SELECT updated_at FROM gateway_rate_buckets WHERE name LIKE :prefix"),
                    {"prefix": prefix + "%"},
                )
                .scalars()
                .all()
            )
            assert len(updated) == 2 and min(updated) >= released_at
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM gateway_rate_buckets WHERE name LIKE :prefix"),
                {"prefix": prefix + "%"},
            )
        engine.dispose()
        other.dispose()
