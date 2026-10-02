"""Production entrypoint: `uvicorn controlplane.main:app_factory --factory`."""

from __future__ import annotations

import atexit

from fastapi import FastAPI

from controlplane import observability
from controlplane.adapters.metrics import PrometheusMetricsProvider
from controlplane.adapters.mlflow import MlflowExperimentProvider
from controlplane.adapters.serving import KServeServingProvider
from controlplane.adapters.workflow import ArgoWorkflowProvider
from controlplane.api.app import create_app
from controlplane.observability import observe, observed_uow_factory
from controlplane.persistence.sql import SqlUnitOfWork, make_engine, sql_uow_factory
from controlplane.settings import Settings

SERVICE_NAME = "mlp-controlplane-api"


def app_factory() -> FastAPI:
    settings = Settings()
    engine = make_engine(settings.database_url)
    telemetry = observability.configure(
        SERVICE_NAME, json_logs=settings.log_json, log_level=settings.log_level, engine=engine
    )
    atexit.register(telemetry.shutdown)  # flush the last spans and metrics on a clean exit

    sessions = sql_uow_factory(engine)
    workflow = observe(
        ArgoWorkflowProvider.from_kubeconfig(settings.kubeconfig or None),
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
    )
