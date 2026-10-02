"""FastAPI entrypoint for the inference service."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, MutableMapping
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from fastapi.telemetry import TelemetryConfig
from opentelemetry import trace

from app import metrics
from app.config import settings
from app.inference import service
from app.schemas import (
    HealthResponse,
    PredictRequest,
    PredictResponse,
    ReadyResponse,
)


def _add_trace_context(
    _: Any, __: str, event: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Put the active trace on every log line, so a log can be opened as a trace."""
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event.setdefault("trace_id", f"{ctx.trace_id:032x}")
        event.setdefault("span_id", f"{ctx.span_id:016x}")
    return event


def _skip_telemetry(scope: MutableMapping[str, Any]) -> bool:
    """Probes and scrapes run every few seconds; they are not worth a trace each."""
    return scope.get("path") in {"/health", "/ready", "/metrics"}


def _configure_logging() -> None:
    logging.basicConfig(level=settings.log_level, format="%(message)s")
    renderer = (
        structlog.processors.JSONRenderer()
        if settings.log_json
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _add_trace_context,
            renderer,
        ]
    )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    _configure_logging()
    # Non-blocking: the server must answer /health even while the model is
    # still downloading, or a liveness probe would kill a healthy process.
    service.start()
    try:
        yield
    finally:
        service.stop()


# FastAPI's native OpenTelemetry. It exports only when an OTEL_EXPORTER_OTLP_* endpoint is
# set (OTLP over HTTP/protobuf, configured from the standard OTEL_* variables); otherwise it
# is a no-op. Prometheus metrics stay as they are: they are what the alerts are written on.
_telemetry: TelemetryConfig = {"exclude": _skip_telemetry, "logs": False}
app = FastAPI(title=settings.app_name, lifespan=lifespan, telemetry=_telemetry)
app.middleware("http")(metrics.metrics_middleware)


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    return metrics.metrics_response()


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@app.get("/ready")
def ready() -> JSONResponse:
    ready_now = service.ready
    body = ReadyResponse(
        ready=ready_now,
        state=service.state,
        model_version=service.model_version,
        model_source=service.model_source,
        detail=service.error,
    )
    status = 200 if ready_now else 503
    return JSONResponse(status_code=status, content=body.model_dump())


@app.post("/predict", response_model=PredictResponse)
def predict(req: PredictRequest) -> PredictResponse:
    if not service.ready:
        raise HTTPException(status_code=503, detail="model not ready")
    with trace.get_tracer("app.inference").start_as_current_span(
        "model.predict",
        attributes={
            "ml.model.version": service.model_version or "unknown",
            "ml.features.count": len(req.features),
        },
    ):
        value = service.predict(req.features)
    version = service.model_version or "unknown"
    return PredictResponse(prediction=value, model_version=version)
