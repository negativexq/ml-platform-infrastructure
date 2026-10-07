"""Low-cardinality database metrics, without SQL text, parameters or credentials."""

from time import perf_counter
from typing import Any

from opentelemetry import metrics
from opentelemetry.metrics import Observation
from sqlalchemy import Engine, event

from controlplane.observability.profile import sample_latency
from controlplane.persistence.pool import TimedQueuePool

_BUCKETS = [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 6]


def instrument_database(engine: Engine, *, latency_sample_rate: float = 1) -> None:
    meter = metrics.get_meter("controlplane.database")
    attrs = {"db.system": engine.dialect.name}

    def histogram(name: str, description: str) -> Any:
        return meter.create_histogram(
            name,
            unit="s",
            description=description,
            explicit_bucket_boundaries_advisory=_BUCKETS,
        )

    acquire = histogram("mlp.db.pool.acquire.duration", "Pool queue + connect + pre-ping")
    query = histogram("mlp.db.query.duration", "DBAPI execute duration; excludes result fetching")
    transaction = histogram(
        "mlp.db.transaction.duration", "Transaction lifetime until commit/rollback begins"
    )
    errors = meter.create_counter("mlp.db.errors", description="Exact DB errors by operation")
    connections = meter.create_counter("mlp.db.pool.connections", unit="{connection}")
    invalidations = meter.create_counter("mlp.db.pool.invalidations", unit="{connection}")

    if isinstance(engine.pool, TimedQueuePool):

        def observe_acquire(seconds: float, outcome: str) -> None:
            if outcome == "error":
                errors.add(1, {**attrs, "operation": "acquire"})
            if sample_latency(latency_sample_rate):
                acquire.record(seconds, {**attrs, "outcome": outcome})

        engine.pool.observe_acquire = observe_acquire

        def checked_out(options: Any) -> list[Observation]:
            pool = engine.pool
            return (
                [Observation(pool.checkedout(), attrs)] if isinstance(pool, TimedQueuePool) else []
            )

        def idle(options: Any) -> list[Observation]:
            pool = engine.pool
            return (
                [Observation(pool.checkedin(), attrs)] if isinstance(pool, TimedQueuePool) else []
            )

        def capacity(options: Any) -> list[Observation]:
            pool = engine.pool
            return [Observation(pool.capacity, attrs)] if isinstance(pool, TimedQueuePool) else []

        meter.create_observable_gauge("mlp.db.pool.checked_out", callbacks=[checked_out])
        meter.create_observable_gauge("mlp.db.pool.capacity", callbacks=[capacity])
        meter.create_observable_gauge("mlp.db.pool.idle", callbacks=[idle])

    @event.listens_for(engine, "connect")
    def connected(dbapi: Any, record: Any) -> None:
        connections.add(1, attrs)

    @event.listens_for(engine, "invalidate")
    def invalidated(dbapi: Any, record: Any, exception: Any) -> None:
        invalidations.add(1, attrs)

    @event.listens_for(engine, "before_cursor_execute")
    def before(
        conn: Any, cursor: Any, statement: Any, parameters: Any, context: Any, executemany: Any
    ) -> None:
        context._mlp_query_started = perf_counter()

    def finish_query(context: Any, outcome: str) -> None:
        started = getattr(context, "_mlp_query_started", None)
        context._mlp_query_started = None
        if started is not None:
            if outcome == "error":
                errors.add(1, {**attrs, "operation": "query"})
            if sample_latency(latency_sample_rate):
                query.record(perf_counter() - started, {**attrs, "outcome": outcome})

    @event.listens_for(engine, "after_cursor_execute")
    def after(
        conn: Any, cursor: Any, statement: Any, parameters: Any, context: Any, executemany: Any
    ) -> None:
        finish_query(context, "ok")

    @event.listens_for(engine, "handle_error")
    def failed(context: Any) -> None:
        if context.execution_context is not None:
            finish_query(context.execution_context, "error")

    @event.listens_for(engine, "begin")
    def begin(conn: Any) -> None:
        # Connection.info can reconnect/raise after invalidation; instrumentation
        # must not interfere with rollback of a disconnected transaction.
        conn._mlp_transaction_started = perf_counter()

    def finish_transaction(conn: Any, outcome: str) -> None:
        started = getattr(conn, "_mlp_transaction_started", None)
        conn._mlp_transaction_started = None
        if started is not None and sample_latency(latency_sample_rate):
            transaction.record(perf_counter() - started, {**attrs, "outcome": outcome})

    event.listen(engine, "commit", lambda conn: finish_transaction(conn, "commit"))
    event.listen(engine, "rollback", lambda conn: finish_transaction(conn, "rollback"))
