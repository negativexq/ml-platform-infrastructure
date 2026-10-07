"""Traces, metrics and logs: one trace follows a request through the API, the database and
the reconciler, and observability never reports a change that did not happen."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from controlplane.adapters.fakes import FakeClusterProvider
from controlplane.api.app import create_app
from controlplane.application import context
from controlplane.application.ports import UnitOfWork
from controlplane.domain.audit import AuditEvent
from controlplane.domain.errors import Conflict
from controlplane.observability import (
    CLUSTER_MUTATIONS,
    instrument_reconciler,
    observe,
    observed_uow_factory,
)
from controlplane.observability.log import add_trace_context
from controlplane.observability.tracing import otel_traceparent
from controlplane.reconciliation.projects import ProjectReconciler
from controlplane.tests.conftest import FakeClock

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
INCOMING = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


@pytest.fixture(scope="session")
def telemetry() -> Iterator[tuple[InMemorySpanExporter, InMemoryMetricReader]]:
    """Global providers (the code under test uses the global tracer and meter), set once."""
    exporter, reader = InMemorySpanExporter(), InMemoryMetricReader()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    context.set_traceparent_provider(otel_traceparent)
    yield exporter, reader
    context.set_traceparent_provider(lambda: None)


@pytest.fixture
def spans(telemetry: Any) -> InMemorySpanExporter:
    telemetry[0].clear()
    return telemetry[0]  # type: ignore[no-any-return]


@pytest.fixture
def reader(telemetry: Any) -> InMemoryMetricReader:
    return telemetry[1]  # type: ignore[no-any-return]


@pytest.fixture
def observed(uow_factory: Callable[[], UnitOfWork]) -> Callable[[], UnitOfWork]:
    return observed_uow_factory(uow_factory)


def _counter(reader: InMemoryMetricReader, name: str) -> dict[tuple[tuple[str, Any], ...], float]:
    out: dict[tuple[tuple[str, Any], ...], float] = {}
    data = reader.get_metrics_data()
    for resource in data.resource_metrics if data else []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    for point in metric.data.data_points:
                        key = tuple(sorted(dict(point.attributes or {}).items()))
                        out[key] = out.get(key, 0) + getattr(point, "value", 0)
    return out


def _named(spans: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [s for s in spans.get_finished_spans() if s.name == name]


def _reconciler(
    observed: Callable[[], UnitOfWork], cluster: FakeClusterProvider, clock: FakeClock
) -> ProjectReconciler:
    reconciler = ProjectReconciler(observed, observe(cluster, "cluster", CLUSTER_MUTATIONS), clock)

    def origin(uow: UnitOfWork, entity_id: UUID) -> str | None:
        entity = uow.projects.get(entity_id)
        return None if entity is None else entity.traceparent

    instrument_reconciler(reconciler, "projects", observed, origin)
    return reconciler


def test_gateway_server_to_httpx_trace(spans: InMemorySpanExporter) -> None:
    import asyncio

    import httpx
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    from controlplane.adapters.gateway import HttpUpstream
    from controlplane.application.gateway import GatewayReply, UpstreamCall
    from controlplane.domain.states import EndpointProtocol
    from controlplane.gateway import create_gateway

    propagated: list[str] = []

    def backend(request: httpx.Request) -> httpx.Response:
        propagated.append(request.headers["traceparent"])
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"prediction": 1})

    upstream = HttpUpstream(httpx.AsyncClient(transport=httpx.MockTransport(backend)))
    HTTPXClientInstrumentor.instrument_client(upstream.client)

    class Service:
        async def invoke(self, **kwargs: Any) -> GatewayReply:
            reply = await upstream.call(
                UpstreamCall(
                    "mlp-test/function",
                    "http://backend",
                    EndpointProtocol.HTTP,
                    kwargs["body"],
                    5,
                    kwargs["request_id"],
                )
            )
            return GatewayReply(reply.status, reply.content_type, reply.chunks)

    try:
        with TestClient(create_gateway(Service())) as client:  # type: ignore[arg-type]
            assert client.get("/healthz").status_code == 200
            assert client.get("/readyz").status_code == 200
            assert not spans.get_finished_spans()
            reply = client.post(
                "/v1/test/function/invoke", content=b"{}", headers={"traceparent": INCOMING}
            )
            assert reply.status_code == 200
        finished = spans.get_finished_spans()
        server = next(s for s in finished if s.kind == trace.SpanKind.SERVER)
        outbound = next(s for s in finished if s.kind == trace.SpanKind.CLIENT)
        assert server.context.trace_id == outbound.context.trace_id == int(TRACE_ID, 16)
        assert outbound.parent is not None
        assert propagated == [f"00-{TRACE_ID}-{outbound.context.span_id:016x}-01"]
    finally:
        HTTPXClientInstrumentor.uninstrument_client(upstream.client)
        asyncio.run(upstream.aclose())


def test_database_metrics_include_pool_timeout_and_errors(reader: InMemoryMetricReader) -> None:
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError, TimeoutError

    from controlplane.observability.database import instrument_database
    from controlplane.persistence.pool import TimedQueuePool

    engine = create_engine(
        "sqlite://", poolclass=TimedQueuePool, pool_size=1, max_overflow=0, pool_timeout=0.05
    )
    instrument_database(engine)
    try:
        with engine.connect() as conn:
            with pytest.raises(TimeoutError):
                engine.connect()
            conn.execute(text("SELECT 1"))
            conn.commit()
            with pytest.raises(OperationalError):
                conn.execute(text("SELECT * FROM missing_table"))
            conn.rollback()
            conn.execute(text("SELECT 1"))
            conn.invalidate()
            conn.rollback()  # telemetry must not reconnect a disconnected transaction
        # Engine.dispose replaces its pool; the observer must survive replacement.
        engine.dispose()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        points: dict[str, list[Any]] = {}
        data = reader.get_metrics_data()
        assert data is not None
        for resource in data.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    points.setdefault(metric.name, []).extend(metric.data.data_points)
        acquire = points["mlp.db.pool.acquire.duration"]
        error = next(p for p in acquire if p.attributes["outcome"] == "error")
        assert error.count == 1 and error.sum >= 0.04
        assert next(p for p in acquire if p.attributes["outcome"] == "ok").count == 2
        assert {p.attributes["outcome"] for p in points["mlp.db.query.duration"]} == {"ok", "error"}
        assert {p.attributes["outcome"] for p in points["mlp.db.transaction.duration"]} == {
            "commit",
            "rollback",
        }
        assert points["mlp.db.pool.checked_out"][0].value == 0
        assert points["mlp.db.pool.capacity"][0].value == 1
        assert all(
            set(p.attributes) <= {"db.system", "outcome", "operation"}
            for name, values in points.items()
            if name.startswith("mlp.db.")
            for p in values
        )
    finally:
        engine.dispose()


def test_reconciler_queries_join_origin_trace(
    spans: InMemorySpanExporter, uow_factory: Callable[[], UnitOfWork]
) -> None:
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    from sqlalchemy import create_engine, text

    engine = create_engine("sqlite://")
    instrumentor = SQLAlchemyInstrumentor()
    instrumentor.instrument(engine=engine)

    class Reconciler:
        def reconcile(self, entity_id: UUID) -> None:
            with engine.begin() as conn:
                conn.execute(text("SELECT 1"))

        def reconcile_all(self) -> None:
            self.reconcile(uuid4())

    try:
        reconciler = instrument_reconciler(
            Reconciler(), "test-db", uow_factory, lambda uow, entity_id: INCOMING
        )
        reconciler.reconcile_all()
        queries = [
            s for s in spans.get_finished_spans() if (s.attributes or {}).get("db.statement")
        ]
        assert queries and {s.context.trace_id for s in queries} == {int(TRACE_ID, 16)}
        assert not trace.get_current_span().get_span_context().is_valid
    finally:
        instrumentor.uninstrument()
        engine.dispose()


def test_pod_resource_defaults_preserve_explicit_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from controlplane.observability.setup import _resource

    monkeypatch.setenv("MLP_POD_UID", "pod-uid")
    monkeypatch.setenv("MLP_POD_NAME", "gateway-abc")
    monkeypatch.setenv("MLP_POD_NAMESPACE", "mlp-system")
    monkeypatch.setenv("MLP_NODE_NAME", "node-a")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.instance.id=explicit")
    resource = _resource("mlp-gateway").attributes
    assert resource["service.instance.id"] == "explicit"
    assert resource["k8s.pod.uid"] == "pod-uid"
    assert resource["k8s.pod.name"] == "gateway-abc"
    assert resource["k8s.namespace.name"] == "mlp-system"
    assert resource["k8s.node.name"] == "node-a"


def test_one_trace_from_request_to_reconciler(
    observed: Callable[[], UnitOfWork], clock: FakeClock, spans: InMemorySpanExporter
) -> None:
    client = TestClient(create_app(observed, clock))
    created = client.post(
        "/projects", json={"name": "credit-risk"}, headers={"traceparent": INCOMING}
    )
    assert created.status_code == 201
    project_id = UUID(created.json()["id"])

    # later, in another process with no request around: the reconciler joins the same trace
    reconciler = _reconciler(observed, FakeClusterProvider(), clock)
    assert reconciler.reconcile(project_id).after.value == "READY"

    finished = spans.get_finished_spans()
    assert {s.context.trace_id for s in finished} == {int(TRACE_ID, 16)}
    names = {s.name for s in finished}
    assert {
        "POST /projects",
        "project.created",
        "project.provisioning",
        "cluster.apply",
        "project.provisioned",
    } <= names
    assert "cluster.observe" not in names  # reads are metrics, not spans

    with observed() as uow:
        events = uow.audit.list(entity_id=project_id)
    assert events and {e.trace_id for e in events} == {TRACE_ID}


def test_audit_event_without_a_trace_has_no_trace_id(
    observed: Callable[[], UnitOfWork], clock: FakeClock
) -> None:
    client = TestClient(create_app(observed, clock))
    context.set_traceparent_provider(lambda: None)
    try:
        project_id = UUID(client.post("/projects", json={"name": "abc"}).json()["id"])
    finally:
        context.set_traceparent_provider(otel_traceparent)
    with observed() as uow:
        assert [e.trace_id for e in uow.audit.list(entity_id=project_id)][-1:] != [TRACE_ID]


def test_no_span_for_a_change_that_rolled_back(
    observed: Callable[[], UnitOfWork], spans: InMemorySpanExporter, clock: FakeClock
) -> None:
    class Boom(Exception):
        pass

    with pytest.raises(Boom), observed() as uow:
        uow.audit.record(_event(clock))
        raise Boom
    assert not _named(spans, "project.created")


def _event(clock: FakeClock) -> AuditEvent:
    return AuditEvent(
        id=UUID(int=2),
        occurred_at=clock(),
        actor="tester",
        action="project.created",
        entity_type="project",
        entity_id=UUID(int=1),
        project_id=None,
        payload={},
    )


def test_converged_reconcile_emits_nothing(
    observed: Callable[[], UnitOfWork],
    clock: FakeClock,
    spans: InMemorySpanExporter,
    reader: InMemoryMetricReader,
) -> None:
    client = TestClient(create_app(observed, clock))
    project_id = UUID(client.post("/projects", json={"name": "calm"}).json()["id"])
    reconciler = _reconciler(observed, FakeClusterProvider(), clock)
    reconciler.reconcile(project_id)
    spans.clear()
    before = _counter(reader, "mlp.reconcile.runs")

    reconciler.reconcile(project_id)

    assert not [s for s in spans.get_finished_spans() if s.name.startswith("project.")]
    key = (("outcome", "unchanged"), ("reconciler", "projects"))
    after = _counter(reader, "mlp.reconcile.runs")
    assert after[key] == before.get(key, 0) + 1


def test_provider_errors_are_counted_and_marked(
    observed: Callable[[], UnitOfWork],
    clock: FakeClock,
    spans: InMemorySpanExporter,
    reader: InMemoryMetricReader,
) -> None:
    cluster = FakeClusterProvider()
    cluster.fail_apply = ConnectionError("api server down")
    client = TestClient(create_app(observed, clock))
    project_id = UUID(client.post("/projects", json={"name": "broken"}).json()["id"])
    _reconciler(observed, cluster, clock).reconcile(project_id)

    (apply_span,) = _named(spans, "cluster.apply")
    assert apply_span.status.status_code is StatusCode.ERROR
    errors = _counter(reader, "mlp.provider.calls")
    assert errors[(("operation", "apply"), ("outcome", "error"), ("provider", "cluster"))] >= 1
    assert _named(spans, "project.failed")[0].status.status_code is StatusCode.ERROR


def test_reconciler_outcomes(
    observed: Callable[[], UnitOfWork], reader: InMemoryMetricReader
) -> None:
    class Stub:
        def __init__(self, behaviour: Callable[[UUID], Any]) -> None:
            self.behaviour = behaviour
            self.passes = 0

        def reconcile(self, entity_id: UUID) -> Any:
            return self.behaviour(entity_id)

        def reconcile_all(self) -> list[Any]:
            self.passes += 1
            return []

    class Result:
        def __init__(self, before: str, after: str) -> None:
            self.before, self.after = before, after

    def raising(exc: Exception) -> Callable[[UUID], Any]:
        def go(_: UUID) -> Any:
            raise exc

        return go

    name = f"stub-{uuid4().hex[:8]}"
    stub = instrument_reconciler(Stub(lambda _: Result("a", "b")), name, observed)
    stub.reconcile(UUID(int=1))
    stub.behaviour = lambda _: Result("a", "a")
    stub.reconcile(UUID(int=1))
    stub.behaviour = raising(Conflict("raced"))
    with pytest.raises(Conflict):
        stub.reconcile(UUID(int=1))
    stub.behaviour = raising(RuntimeError("x"))
    with pytest.raises(RuntimeError):
        stub.reconcile(UUID(int=1))
    stub.reconcile_all()

    runs = _counter(reader, "mlp.reconcile.runs")
    for outcome in ("changed", "unchanged", "conflict", "error"):
        assert runs[(("outcome", outcome), ("reconciler", name))] == 1
    assert _counter(reader, "mlp.reconcile.passes")[(("reconciler", name),)] == 1
    assert context.bound_origin() is None  # always cleared


def test_http_404_and_422_are_not_errors_but_500_is(
    observed: Callable[[], UnitOfWork], clock: FakeClock, spans: InMemorySpanExporter
) -> None:
    app = create_app(observed, clock)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("bug")

    client = TestClient(app, raise_server_exceptions=False)
    assert client.get(f"/projects/{UUID(int=9)}").status_code == 404
    assert client.post("/projects", json={"name": "Not Valid"}).status_code == 422
    assert client.get("/boom").status_code == 500

    finished = spans.get_finished_spans()
    assert not [
        s
        for s in finished
        if s.name.startswith("GET /projects") or s.name == "POST /projects"
        if s.status.status_code is StatusCode.ERROR and s.name != "GET /boom"
    ]
    assert any(s.status.status_code is StatusCode.ERROR and "boom" in s.name for s in finished), [
        s.name for s in finished
    ]


def test_log_lines_carry_the_active_trace(spans: InMemorySpanExporter) -> None:
    with trace.get_tracer("t").start_as_current_span("work") as span:
        event = add_trace_context(None, "info", {"event": "hello"})
        assert event["trace_id"] == f"{span.get_span_context().trace_id:032x}"
        assert event["span_id"] == f"{span.get_span_context().span_id:016x}"
    assert "trace_id" not in add_trace_context(None, "info", {"event": "idle"})


def _run(code: str, **env: str) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    return subprocess.run(
        [sys.executable, "-c", code],
        env={**clean, **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_export_is_off_unless_an_endpoint_is_configured() -> None:
    code = (
        "from controlplane.observability.setup import setup_providers as s; print(s('x').enabled)"
    )
    assert _run(code).stdout.strip() == "False"
    on = _run(code, OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:1")
    assert on.stdout.strip() == "True", on.stderr
    off = _run(code, OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:1", OTEL_SDK_DISABLED="true")
    assert off.stdout.strip() == "False"


def test_grpc_is_rejected_with_a_clear_message() -> None:
    result = _run(
        "from controlplane.observability.setup import setup_providers as s; s('x')",
        OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:1",
        OTEL_EXPORTER_OTLP_PROTOCOL="grpc",
    )
    assert result.returncode != 0 and "http/protobuf" in result.stderr


def test_gateway_usage_becomes_metrics(reader: InMemoryMetricReader) -> None:
    from controlplane.application.gateway import CallRecord
    from controlplane.observability.metrics import GatewayUsageMetrics

    before = _counter(reader, "mlp.gateway.requests")
    usage = GatewayUsageMetrics()
    usage.record(CallRecord("credit-risk", "prod", "partner-acme", 200, 1, "requests", 0.05))
    usage.record(CallRecord("credit-risk", "prod", "partner-acme", 429, 0, "requests", 0.001))
    after = _counter(reader, "mlp.gateway.requests")
    where = (("caller", "partner-acme"), ("endpoint", "prod"), ("project", "credit-risk"))
    for code in ("200", "429"):
        key = tuple(sorted((*(pair for pair in where if pair[0] != "caller"), ("code", code))))
        assert after[key] - before.get(key, 0) == 1
    units = _counter(reader, "mlp.gateway.units")
    assert units[tuple(sorted((*where, ("unit", "requests"))))] >= 1


def test_gateway_runtime_tracks_full_stream_and_shuts_down(reader: InMemoryMetricReader) -> None:
    import asyncio
    import time

    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse

    from controlplane.observability.gateway import instrument_runtime

    app = FastAPI()

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def chunks() -> Any:
            time.sleep(0.15)  # Deliberately block the loop so the lag probe observes it.
            await asyncio.sleep(0.15)
            yield b"done"

        return StreamingResponse(chunks())

    instrument_runtime(app)
    with TestClient(app) as client:
        client.get("/stream")
    points: dict[str, list[Any]] = {}
    data = reader.get_metrics_data()
    assert data is not None
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                points.setdefault(metric.name, []).extend(metric.data.data_points)
    assert points["mlp.gateway.inflight"][0].value == 0
    assert points["mlp.gateway.event_loop.lag"][0].sum > 0.02
    assert points["mlp.gateway.anyio.threadpool.capacity"][0].value > 0


@pytest.mark.parametrize("signal", ["traces", "metrics"])
def test_otel_signals_can_be_enabled_independently(signal: str) -> None:
    code = (
        "from controlplane.observability.setup import setup_providers; "
        "t=setup_providers('x'); "
        "print(t.tracer_provider is not None, t.meter_provider is not None); t.shutdown()"
    )
    only = _run(code, **{f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT": "http://127.0.0.1:1"})
    assert only.returncode == 0, only.stderr
    assert only.stdout.strip() == ("True False" if signal == "traces" else "False True")
    disabled = _run(
        code,
        OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:1",
        **{f"OTEL_{signal.upper()}_EXPORTER": "none"},
    )
    assert disabled.returncode == 0, disabled.stderr
    assert disabled.stdout.strip() == ("False True" if signal == "traces" else "True False")


def test_latency_sampling_preserves_exact_usage(reader: InMemoryMetricReader) -> None:
    from controlplane.application.gateway import CallRecord
    from controlplane.observability.metrics import GatewayUsageMetrics

    usage = GatewayUsageMetrics(latency_sample_rate=0)
    before = _counter(reader, "mlp.gateway.requests")
    for code, units in [(200, 5), (429, 0), (503, 0)]:
        usage.record(
            CallRecord(
                "sample-project",
                "fn",
                "customer",
                code,
                units,
                "tokens",
                0.01,
                prompt_tokens=3 if code == 200 else 0,
                completion_tokens=2 if code == 200 else 0,
            )
        )
    after = _counter(reader, "mlp.gateway.requests")
    where = (("endpoint", "fn"), ("project", "sample-project"))
    for code in (200, 429, 503):
        key = tuple(sorted((*where, ("code", str(code)))))
        assert after[key] - before.get(key, 0) == 1
    caller = (*where, ("caller", "customer"))
    rejected = _counter(reader, "mlp.gateway.usage.requests")
    assert rejected[tuple(sorted((*caller, ("code", "429"))))] == 1
    assert rejected[tuple(sorted((*caller, ("code", "503"))))] == 1
    assert _counter(reader, "mlp.gateway.units")[tuple(sorted((*caller, ("unit", "tokens"))))] == 5
    tokens = _counter(reader, "mlp.gateway.tokens")
    assert tokens[tuple(sorted((*caller, ("direction", "prompt"))))] == 3
    assert tokens[tuple(sorted((*caller, ("direction", "completion"))))] == 2


def test_gateway_profile_validates_and_separates_diagnostics(monkeypatch: Any) -> None:
    from controlplane.observability.profile import GatewayTelemetryProfile

    for key in list(os.environ):
        if key.startswith("CP_GATEWAY_") or key == "OTEL_METRIC_EXPORT_INTERVAL":
            monkeypatch.delenv(key)
    normal = GatewayTelemetryProfile.from_env()
    assert not normal.sql_tracing and not normal.native_http_metrics
    assert normal.trace_sample_rate == 0.01 and normal.latency_sample_rate == 0.2
    assert normal.export_interval_ms == 30000
    monkeypatch.setenv("CP_GATEWAY_OBSERVABILITY_PROFILE", "diagnostic")
    diagnostic = GatewayTelemetryProfile.from_env()
    assert diagnostic.sql_tracing and diagnostic.operation_spans
    assert diagnostic.trace_sample_rate == diagnostic.latency_sample_rate == 1
    monkeypatch.setenv("CP_GATEWAY_LATENCY_SAMPLE_RATE", "nan")
    with pytest.raises(ValueError, match="between 0 and 1"):
        GatewayTelemetryProfile.from_env()


def test_sql_spans_can_be_disabled_without_losing_db_metrics(
    monkeypatch: Any,
    reader: InMemoryMetricReader,
    spans: InMemorySpanExporter,
) -> None:
    from sqlalchemy import create_engine, text

    from controlplane import observability
    from controlplane.observability.setup import Telemetry

    monkeypatch.setattr(
        observability,
        "setup_providers",
        lambda *args, **kwargs: Telemetry(
            True, trace.get_tracer_provider(), metrics.get_meter_provider()
        ),
    )
    monkeypatch.setattr(observability, "configure_logging", lambda *args, **kwargs: None)

    def query_count() -> int:
        data = reader.get_metrics_data()
        return (
            sum(
                p.count
                for r in data.resource_metrics
                for s in r.scope_metrics
                for m in s.metrics
                if m.name == "mlp.db.query.duration"
                for p in m.data.data_points
            )
            if data
            else 0
        )

    before = query_count()
    engine = create_engine("sqlite://")
    observability.configure("sql-off", engine=engine, sql_tracing=False)
    with trace.get_tracer("test").start_as_current_span("request"), engine.begin() as conn:
        conn.execute(text("SELECT 1"))
    assert [s.name for s in spans.get_finished_spans()] == ["request"]
    data = reader.get_metrics_data()
    assert data is not None
    query = [
        p
        for r in data.resource_metrics
        for s in r.scope_metrics
        for m in s.metrics
        if m.name == "mlp.db.query.duration"
        for p in m.data.data_points
    ]
    assert sum(p.count for p in query) - before == 1
    engine.dispose()
