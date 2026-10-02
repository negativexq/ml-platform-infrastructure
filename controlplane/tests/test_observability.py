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
        key = tuple(sorted((*where, ("code", code))))
        assert after[key] - before.get(key, 0) == 1
    units = _counter(reader, "mlp.gateway.units")
    assert units[tuple(sorted((*where, ("unit", "requests"))))] >= 1
