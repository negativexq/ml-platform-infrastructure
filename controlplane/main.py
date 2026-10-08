"""Production entrypoint: `uvicorn controlplane.main:app_factory --factory`."""

from __future__ import annotations

import atexit
import logging

from fastapi import FastAPI

from controlplane import observability
from controlplane.adapters.identity import OidcProvider
from controlplane.adapters.kubernetes import load_api_client
from controlplane.adapters.kubernetes.secrets import KubernetesSecretProvider
from controlplane.adapters.metrics import (
    PrometheusMetricsProvider,
    PrometheusPlatformTelemetry,
    PrometheusUsage,
)
from controlplane.adapters.mlflow import MlflowExperimentProvider
from controlplane.adapters.serving import KServeServingProvider
from controlplane.adapters.workflow import ArgoWorkflowProvider
from controlplane.api.app import create_app
from controlplane.api.auth import AuthConfig
from controlplane.api.session import Signer
from controlplane.observability import observe, observed_uow_factory
from controlplane.persistence.readiness import DatabaseReadiness
from controlplane.persistence.sql import SqlUnitOfWork, make_engine, sql_uow_factory
from controlplane.settings import Settings

SERVICE_NAME = "mlp-controlplane-api"
log = logging.getLogger(__name__)


def app_factory() -> FastAPI:
    settings = Settings()
    engine = make_engine(settings.database_url)
    if settings.database_role_enforcement:
        DatabaseReadiness(engine, component="api")()
    telemetry = observability.configure(
        SERVICE_NAME, json_logs=settings.log_json, log_level=settings.log_level, engine=engine
    )
    atexit.register(telemetry.shutdown)  # flush the last spans and metrics on a clean exit

    sessions = sql_uow_factory(engine)
    workflow = observe(
        ArgoWorkflowProvider.from_kubeconfig(
            settings.kubeconfig or None, require_image_digest=settings.job_image_digest_required
        ),
        "workflow",
        observability.WORKFLOW_MUTATIONS,
    )
    serving = observe(
        KServeServingProvider.from_kubeconfig(settings.kubeconfig or None),
        "serving",
        observability.SERVING_MUTATIONS,
    )
    experiments = (
        observe(
            MlflowExperimentProvider(settings.mlflow_tracking_uri),
            "experiments",
            observability.EXPERIMENT_MUTATIONS,
        )
        if settings.mlflow_tracking_uri
        else None
    )
    metrics = (
        observe(
            PrometheusMetricsProvider(settings.prometheus_url),
            "metrics",
            observability.NO_MUTATIONS,
        )
        if settings.prometheus_url
        else None
    )
    return create_app(
        observed_uow_factory(lambda: SqlUnitOfWork(sessions)),
        workflow=workflow,
        experiments=experiments,
        serving=serving,
        metrics=metrics,
        auth=auth_config(settings),
        platform=(
            observe(
                PrometheusPlatformTelemetry(settings.prometheus_url),
                "metrics",
                observability.NO_MUTATIONS,
            )
            if settings.prometheus_url
            else None
        ),
        usage=(
            observe(PrometheusUsage(settings.prometheus_url), "metrics", observability.NO_MUTATIONS)
            if settings.prometheus_url
            else None
        ),
        gateway_url=settings.gateway_url,
        log_stream_key=settings.log_stream_signing_key,
        log_stream_url=settings.log_stream_url,
        require_job_image_digest=settings.job_image_digest_required,
        batch_image=settings.batch_image,
        readiness=DatabaseReadiness(
            engine, component="api" if settings.database_role_enforcement else None
        ),
        secrets=KubernetesSecretProvider(load_api_client(settings.kubeconfig or None)),
    )


def auth_config(settings: Settings, *, browser_login: bool = True) -> AuthConfig | None:
    """Fail at start-up, with the fix in the message, rather than run open by accident."""
    if settings.auth_mode == "none":
        log.warning("CP_AUTH_MODE=none: no sign-in, every caller is an anonymous platform admin")
        return None
    if not settings.oidc_issuer:
        raise RuntimeError(
            "CP_OIDC_ISSUER is not set. Point it at your OpenID Connect issuer, or set "
            "CP_AUTH_MODE=none to run without sign-in on a laptop."
        )
    provider = OidcProvider(
        settings.oidc_issuer,
        audience=settings.oidc_audience,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        username_claim=settings.oidc_username_claim,
        groups_claim=settings.oidc_groups_claim,
        platform_admins=[s.strip() for s in settings.platform_admins.split(",") if s.strip()],
    )
    web = browser_login and bool(settings.oidc_client_id)
    if web and len(settings.session_secret) < 32:
        raise RuntimeError("browser sign-in needs CP_SESSION_SECRET (32+ random characters)")
    return AuthConfig(
        authenticator=provider,
        login=provider if web else None,
        signer=Signer(settings.session_secret) if web else None,
        public_url=settings.public_url,
        session_hours=settings.session_hours,
        secure_cookies=not settings.public_url.startswith("http://"),
    )
