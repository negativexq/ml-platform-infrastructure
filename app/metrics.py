"""Prometheus instrumentation.

Only metrics this service can actually measure are defined here. Anything the
README claims must be traceable to one of these.

Each pod runs a single uvicorn worker, so the default in-process registry is
correct; a multi-worker setup would need `prometheus_client.multiprocess`.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.requests import Request
from starlette.responses import Response

# -- HTTP ------------------------------------------------------------------

http_requests_total = Counter(
    "http_requests_total",
    "HTTP requests handled, by route and outcome.",
    ["method", "path", "status"],
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency.",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# -- Prediction ------------------------------------------------------------

prediction_requests_total = Counter(
    "prediction_requests_total",
    "Prediction requests that reached the model.",
)

prediction_errors_total = Counter(
    "prediction_errors_total",
    "Prediction requests that failed, by reason.",
    ["reason"],
)

prediction_duration_seconds = Histogram(
    "prediction_duration_seconds",
    "Time spent inside the model's predict call.",
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)

# -- Model -----------------------------------------------------------------

# A labelled counter is not exported until its first increment, which would
# leave a gap in dashboards and make absence indistinguishable from zero.
# Declaring the known reasons up front pins them at 0.
for _reason in ("not_ready", "inference_error"):
    prediction_errors_total.labels(reason=_reason)

model_load_duration_seconds = Histogram(
    "model_load_duration_seconds",
    "Time to fetch and load a model artifact.",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

model_load_failures_total = Counter(
    "model_load_failures_total",
    "Failed model load attempts.",
)

model_ready = Gauge(
    "model_ready",
    "1 when a model is loaded and the service is serving predictions, else 0.",
)

model_info = Gauge(
    "model_info",
    "Always 1; the labels carry the identity of the loaded model.",
    ["model_uri", "source"],
)


def record_model_loaded(version: str, source: str, seconds: float) -> None:
    model_load_duration_seconds.observe(seconds)
    model_ready.set(1)
    # Clear stale label sets so a version change does not leave two series at 1.
    model_info.clear()
    model_info.labels(model_uri=version, source=source).set(1)


def record_model_failed() -> None:
    model_load_failures_total.inc()
    model_ready.set(0)
    model_info.clear()


def metrics_response() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def metrics_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    started = time.perf_counter()
    response = await call_next(request)
    elapsed = time.perf_counter() - started

    # Label by route template rather than raw path, so an unmatched or
    # parameterised URL cannot blow up series cardinality. `route` is only in
    # the scope once routing has run, which is why this reads it after the call.
    route = request.scope.get("route")
    path = getattr(route, "path", None) or "<unmatched>"

    http_request_duration_seconds.labels(request.method, path).observe(elapsed)
    http_requests_total.labels(request.method, path, str(response.status_code)).inc()
    return response
