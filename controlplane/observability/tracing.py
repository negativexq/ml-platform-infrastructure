"""Trace-context helpers and the lifecycle spans.

A **lifecycle span** is emitted for every audit event, i.e. every real state change, when its
transaction commits. Parented to the request that started the work, they turn the audit trail
into a trace you can open:

    POST /projects/x/pipelines/p/runs
      └─ pipeline_run.created              (API process)
      └─ pipeline_run.submitted            (reconciler process, minutes later)
      └─ step_run.running ... step_run.succeeded
      └─ pipeline_run.succeeded

Reconcile passes that change nothing emit no lifecycle spans. When SQL tracing is
enabled, read-only passes can still emit database spans.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.trace import SpanKind, StatusCode

from controlplane.domain.audit import AuditEvent

_TRACER_NAME = "controlplane.lifecycle"
_MAX_ATTR = 200


def otel_traceparent() -> str | None:
    """The W3C traceparent of the span active right now, if any."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get("traceparent")


def parent_context(traceparent: str | None) -> Context | None:
    return propagate.extract({"traceparent": traceparent}) if traceparent else None


def _scalar(value: Any) -> str | int | float | bool | None:
    if isinstance(value, bool | int | float):
        return value
    if value is None:
        return None
    return str(value)[:_MAX_ATTR]


def emit_lifecycle_span(event: AuditEvent, origin: str | None) -> None:
    """One short span for one state change. `origin` is the traceparent the work belongs to
    (a reconciler's entity); None means "the current span" (an API request)."""
    attributes: dict[str, Any] = {
        "mlp.event.action": event.action,
        "mlp.entity.type": event.entity_type,
        "mlp.entity.id": str(event.entity_id),
        "mlp.actor": event.actor,
    }
    if event.project_id is not None:
        attributes["mlp.project.id"] = str(event.project_id)
    for key, value in event.payload.items():
        scalar = _scalar(value)
        if scalar is not None and not isinstance(value, dict | list | tuple):
            attributes[f"mlp.payload.{key}"] = scalar
    span = trace.get_tracer(_TRACER_NAME).start_span(
        event.action, context=parent_context(origin), kind=SpanKind.INTERNAL, attributes=attributes
    )
    if event.action.endswith(".failed"):
        span.set_status(StatusCode.ERROR, str(event.payload.get("reason", "failed")))
    span.end()
