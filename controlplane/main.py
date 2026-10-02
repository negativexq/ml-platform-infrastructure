"""Production entrypoint: `uvicorn controlplane.main:app_factory --factory`."""

from __future__ import annotations

from fastapi import FastAPI

from controlplane.adapters.mlflow import MlflowExperimentProvider
from controlplane.adapters.serving import KServeServingProvider
from controlplane.adapters.workflow import ArgoWorkflowProvider
from controlplane.api.app import create_app
from controlplane.persistence.sql import SqlUnitOfWork, make_engine, sql_uow_factory
from controlplane.settings import Settings


def app_factory() -> FastAPI:
    settings = Settings()
    sessions = sql_uow_factory(make_engine(settings.database_url))
    workflow = ArgoWorkflowProvider.from_kubeconfig(settings.kubeconfig or None)
    experiments = (
        MlflowExperimentProvider(settings.mlflow_tracking_uri)
        if settings.mlflow_tracking_uri
        else None
    )
    serving = KServeServingProvider.from_kubeconfig(settings.kubeconfig or None)
    return create_app(
        lambda: SqlUnitOfWork(sessions),
        workflow=workflow,
        experiments=experiments,
        serving=serving,
    )
