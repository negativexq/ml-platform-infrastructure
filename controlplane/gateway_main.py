"""Gateway entrypoint: `uvicorn controlplane.gateway_main:app_factory --factory --port 8081`.

A separate process (and Deployment) from the control plane's API: prediction traffic scales
on its own and never competes with management calls.
"""

from __future__ import annotations

import atexit
import logging

from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError

from controlplane import observability
from controlplane.adapters.gateway import HttpUpstream, TokenBucketLimiter
from controlplane.adapters.gateway.sql_limits import SqlTokenBucketLimiter
from controlplane.application.gateway import GatewayService
from controlplane.gateway import create_gateway
from controlplane.main import auth_config
from controlplane.observability.gateway import GatewayPhaseMetrics, instrument_runtime
from controlplane.observability.metrics import GatewayLimiterMetrics, GatewayUsageMetrics
from controlplane.persistence.readiness import DatabaseReadiness
from controlplane.persistence.sql import SqlUnitOfWork, make_engine, sql_uow_factory
from controlplane.settings import Settings

SERVICE_NAME = "mlp-gateway"


def app_factory() -> FastAPI:
    import os

    from controlplane.observability.profile import GatewayTelemetryProfile

    profile = GatewayTelemetryProfile.from_env()
    settings = Settings()
    engine = make_engine(settings.database_url)
    if settings.database_role_enforcement:
        DatabaseReadiness(engine, component="gateway")()
    telemetry = observability.configure(
        SERVICE_NAME,
        json_logs=settings.log_json,
        log_level=settings.log_level,
        engine=engine,
        sql_tracing=profile.sql_tracing,
        latency_sample_rate=profile.latency_sample_rate,
        trace_sample_rate=(
            profile.trace_sample_rate
            if "CP_GATEWAY_TRACE_SAMPLE_RATE" in os.environ
            or profile.name == "diagnostic"
            or "OTEL_TRACES_SAMPLER" not in os.environ
            else None
        ),
        metric_export_interval_ms=profile.export_interval_ms,
    )
    atexit.register(telemetry.shutdown)
    # One log line per forwarded call is noise at this volume; metrics and request ids carry it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sessions = sql_uow_factory(engine)
    # Gateway validates bearer tokens; it needs no browser client/session secrets.
    auth = auth_config(settings, browser_login=False)
    phases = (
        GatewayPhaseMetrics(latency_sample_rate=profile.latency_sample_rate)
        if telemetry.meter_provider is not None
        else None
    )
    upstream = HttpUpstream(observe=phases.record if phases else None)
    if telemetry.tracer_provider is not None:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor.instrument_client(upstream.client)
    service = GatewayService(
        lambda: SqlUnitOfWork(sessions),
        upstream,
        SqlTokenBucketLimiter(
            engine,
            observe=GatewayLimiterMetrics(latency_sample_rate=profile.latency_sample_rate).record,
        )
        if settings.gateway_limit_store == "postgres"
        else TokenBucketLimiter(),
        recorders=[
            GatewayUsageMetrics(
                latency_sample_rate=profile.latency_sample_rate,
                request_caller_label=profile.request_caller_label,
            )
        ],
        authenticator=auth.authenticator if auth is not None else None,
        observe_phase=phases.record if phases else None,
        observe_worker=phases.workers.add if phases else None,
    )
    app = create_gateway(
        service,
        store_errors=(SQLAlchemyError,),
        telemetry={
            "metrics": telemetry.meter_provider is not None and profile.native_http_metrics,
            "tracing": telemetry.tracer_provider is not None,
            "operation_spans": profile.operation_spans,
        },
        readiness=DatabaseReadiness(
            engine, component="gateway" if settings.database_role_enforcement else None
        ),
    )
    if telemetry.meter_provider is not None:
        instrument_runtime(app)
    return app
