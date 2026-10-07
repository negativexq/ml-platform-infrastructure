"""Gateway phases and ASGI runtime metrics; no request-derived metric labels."""

import asyncio
from contextlib import asynccontextmanager, suppress
from time import perf_counter
from typing import Any

import anyio.to_thread
from fastapi import FastAPI
from opentelemetry import metrics
from opentelemetry.metrics import Observation
from starlette.types import ASGIApp, Receive, Scope, Send

from controlplane.observability.profile import sample_latency


class GatewayPhaseMetrics:
    def __init__(self, *, latency_sample_rate: float = 1) -> None:
        self.sample_rate = latency_sample_rate
        meter = metrics.get_meter("controlplane.gateway")
        self.duration = meter.create_histogram(
            "mlp.gateway.phase.duration",
            unit="s",
            explicit_bucket_boundaries_advisory=[
                0.001,
                0.005,
                0.01,
                0.025,
                0.05,
                0.1,
                0.25,
                0.5,
                1,
                2,
                5,
                30,
            ],
        )
        self.workers = meter.create_up_down_counter("mlp.gateway.limiter.workers")

    def record(self, phase: str, seconds: float, outcome: str) -> None:
        if sample_latency(self.sample_rate):
            self.duration.record(seconds, {"phase": phase, "outcome": outcome})


class RuntimeMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.inflight = metrics.get_meter("controlplane.gateway").create_up_down_counter(
            "mlp.gateway.inflight"
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in {"/healthz", "/readyz"}:
            await self.app(scope, receive, send)
            return
        self.inflight.add(1)
        try:
            await self.app(scope, receive, send)
        finally:
            self.inflight.add(-1)


def instrument_runtime(app: FastAPI) -> None:
    app.add_middleware(RuntimeMiddleware)
    original = app.router.lifespan_context
    meter = metrics.get_meter("controlplane.gateway")
    lag = meter.create_histogram(
        "mlp.gateway.event_loop.lag",
        unit="s",
        explicit_bucket_boundaries_advisory=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2],
    )
    state: dict[str, float] = {"borrowed": 0, "capacity": 0}
    meter.create_observable_gauge(
        "mlp.gateway.anyio.threadpool.borrowed",
        callbacks=[lambda options: [Observation(state["borrowed"])]],
    )
    meter.create_observable_gauge(
        "mlp.gateway.anyio.threadpool.capacity",
        callbacks=[lambda options: [Observation(state["capacity"])]],
    )

    async def sample() -> None:
        limiter = anyio.to_thread.current_default_thread_limiter()
        while True:
            deadline = perf_counter() + 0.1
            await asyncio.sleep(0.1)
            lag.record(max(0, perf_counter() - deadline))
            state.update(borrowed=limiter.borrowed_tokens, capacity=limiter.total_tokens)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> Any:
        async with original(application):
            task = asyncio.create_task(sample(), name="gateway-runtime-metrics")
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app.router.lifespan_context = lifespan
