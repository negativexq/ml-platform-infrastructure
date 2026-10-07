"""Database engine construction shared by runtime and the migration image."""

from sqlalchemy import Engine, create_engine

from controlplane.persistence.pool import TimedQueuePool


def make_engine(url: str) -> Engine:
    if url.startswith("postgresql"):
        return create_engine(
            url,
            poolclass=TimedQueuePool,
            pool_pre_ping=True,
            pool_timeout=3,
            connect_args={"connect_timeout": 3},
        )
    return create_engine(url, pool_pre_ping=True)
