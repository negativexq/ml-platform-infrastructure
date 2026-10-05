"""Two limiter instances share atomic budgets without starting a database server."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import create_engine

from controlplane.adapters.gateway.sql_limits import SqlTokenBucketLimiter
from controlplane.persistence.models import Base


def test_shared_admission_refund_debt_and_restart(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///" + str(tmp_path / "limits.db"))
    Base.metadata.tables["gateway_rate_buckets"].create(engine)
    first, second = SqlTokenBucketLimiter(engine), SqlTokenBucketLimiter(engine)
    buckets = [("endpoint", 100), ("caller", 100)]
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(
            workers.map(lambda i: (first if i % 2 else second).take(buckets, 10), range(30))
        )
    assert sum(r.allowed for r in results) == 10
    assert not second.take(buckets, 10).allowed
    first.refund(buckets, 25)
    assert second.take(buckets, 20).allowed
    second.charge(buckets, 50)
    assert not first.admit(buckets).allowed
    engine.dispose()
    restarted_engine = create_engine("sqlite:///" + str(tmp_path / "limits.db"))
    assert not SqlTokenBucketLimiter(restarted_engine).take(buckets, 1).allowed
    restarted_engine.dispose()


def test_denial_does_not_consume_other_bucket(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///" + str(tmp_path / "limits.db"))
    Base.metadata.tables["gateway_rate_buckets"].create(engine)
    limiter = SqlTokenBucketLimiter(engine)
    assert limiter.take([("a", 100), ("b", 10)], 10).allowed
    assert not limiter.take([("a", 100), ("b", 10)], 10).allowed
    assert limiter.take([("a", 100)], 90).allowed
    engine.dispose()
