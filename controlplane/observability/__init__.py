"""Observability for the control plane: logs, metrics and traces, wired from one place.

    telemetry = configure("mlp-controlplane-api", engine=engine)   # in a composition root
    uow_factory = observed_uow_factory(uow_factory)
    workflow = observe(workflow, "workflow", WORKFLOW_MUTATIONS)

Only composition roots (`main.py`, `reconciler_main.py`, `demo.py`) import this package. The
domain and application layers never do: they see a trace only as the opaque `traceparent`
string in `controlplane.application.context`.
"""

from __future__ import annotations

from typing import Any

from controlplane.application.context import set_traceparent_provider
from controlplane.observability.log import configure_logging
from controlplane.observability.providers import (
    CLUSTER_MUTATIONS,
    EXPERIMENT_MUTATIONS,
    NO_MUTATIONS,
    SERVING_MUTATIONS,
    WORKFLOW_MUTATIONS,
    observe,
)
from controlplane.observability.reconcile import instrument_reconciler
from controlplane.observability.setup import Telemetry, export_requested, setup_providers
from controlplane.observability.tracing import otel_traceparent
from controlplane.observability.uow import observed_uow_factory


def configure(
    service_name: str,
    *,
    json_logs: bool = True,
    log_level: str = "INFO",
    engine: Any = None,
    sql_tracing: bool = True,
    latency_sample_rate: float = 1,
    trace_sample_rate: float | None = None,
    metric_export_interval_ms: int | None = None,
) -> Telemetry:
    """Set up logging and (if OTEL_EXPORTER_OTLP_ENDPOINT is set) tracing and metrics."""
    configure_logging(service_name, json=json_logs, level=log_level)
    telemetry = setup_providers(
        service_name,
        trace_sample_rate=trace_sample_rate,
        metric_export_interval_ms=metric_export_interval_ms,
    )
    set_traceparent_provider(otel_traceparent)
    if telemetry.tracer_provider is not None and sql_tracing and engine is not None:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=engine)
    if telemetry.meter_provider is not None and engine is not None:
        from controlplane.observability.database import instrument_database

        instrument_database(engine, latency_sample_rate=latency_sample_rate)
    return telemetry


__all__ = [
    "CLUSTER_MUTATIONS",
    "EXPERIMENT_MUTATIONS",
    "NO_MUTATIONS",
    "SERVING_MUTATIONS",
    "WORKFLOW_MUTATIONS",
    "Telemetry",
    "configure",
    "export_requested",
    "instrument_reconciler",
    "observe",
    "observed_uow_factory",
]
