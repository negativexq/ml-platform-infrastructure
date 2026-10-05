"""Gateway entrypoint: `uvicorn controlplane.gateway_main:app_factory --factory --port 8081`.

A separate process (and Deployment) from the control plane's API: prediction traffic scales
on its own and never competes with management calls.
"""

from __future__ import annotations

import atexit
import logging

from fastapi import FastAPI

from controlplane import observability
from controlplane.adapters.gateway import HttpUpstream, TokenBucketLimiter
from controlplane.adapters.gateway.sql_limits import SqlTokenBucketLimiter
from controlplane.application.gateway import GatewayService
from controlplane.gateway import create_gateway
from controlplane.main import auth_config
from controlplane.observability.metrics import GatewayLimiterMetrics, GatewayUsageMetrics
from controlplane.persistence.readiness import DatabaseReadiness
from controlplane.persistence.sql import SqlUnitOfWork, make_engine, sql_uow_factory
from controlplane.settings import Settings

SERVICE_NAME = "mlp-gateway"


def app_factory() -> FastAPI:
    settings = Settings()
    engine = make_engine(settings.database_url)
    telemetry = observability.configure(
        SERVICE_NAME, json_logs=settings.log_json, log_level=settings.log_level, engine=engine
    )
    atexit.register(telemetry.shutdown)
    # One log line per forwarded call is noise at this volume; metrics and request ids carry it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sessions = sql_uow_factory(engine)
    auth = auth_config(settings)  # signed-in callers (client credentials) need the invoker role
    service = GatewayService(
        lambda: SqlUnitOfWork(sessions),
        HttpUpstream(),
        SqlTokenBucketLimiter(engine, observe=GatewayLimiterMetrics().record)
        if settings.gateway_limit_store == "postgres"
        else TokenBucketLimiter(),
        recorders=[GatewayUsageMetrics()],
        authenticator=auth.authenticator if auth is not None else None,
    )
    return create_gateway(service, readiness=DatabaseReadiness(engine))
