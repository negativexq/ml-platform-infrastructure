"""Reconciler entrypoint: `python -m controlplane.reconciler_main`.

Runs the project and run reconcilers in one loop. Each pass is independent: a
failure in one reconciler is logged and does not stop the other.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from controlplane.adapters.kubernetes import KubernetesClusterProvider, load_api_client
from controlplane.adapters.metrics import PrometheusMetricsProvider
from controlplane.adapters.mlflow import MlflowExperimentProvider
from controlplane.adapters.serving import KServeServingProvider
from controlplane.adapters.workflow import ArgoWorkflowProvider
from controlplane.persistence.sql import SqlUnitOfWork, make_engine, sql_uow_factory
from controlplane.reconciliation.deployments import DeploymentReconciler
from controlplane.reconciliation.model_aliases import ModelAliasReconciler
from controlplane.reconciliation.pipeline_runs import PipelineRunReconciler
from controlplane.reconciliation.projects import ProjectReconciler
from controlplane.reconciliation.rollouts import RolloutReconciler
from controlplane.reconciliation.runs import RunReconciler
from controlplane.settings import Settings

log = logging.getLogger("controlplane.reconciler")


def _pass(name: str, step: Callable[[], list[object]]) -> None:
    try:
        for result in step():
            log.info("%s: %s", name, result)
    except Exception:  # noqa: BLE001 - one bad pass must not kill the loop
        log.exception("%s pass failed", name)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    sessions = sql_uow_factory(make_engine(settings.database_url))
    api = load_api_client(settings.kubeconfig or None)
    projects = ProjectReconciler(lambda: SqlUnitOfWork(sessions), KubernetesClusterProvider(api))
    runs = RunReconciler(lambda: SqlUnitOfWork(sessions), ArgoWorkflowProvider(api))
    experiments = (
        MlflowExperimentProvider(settings.mlflow_tracking_uri)
        if settings.mlflow_tracking_uri
        else None
    )
    pipeline_runs = PipelineRunReconciler(
        lambda: SqlUnitOfWork(sessions),
        ArgoWorkflowProvider(api),
        experiments,
        tracking_uri=settings.step_mlflow_tracking_uri or settings.mlflow_tracking_uri or None,
    )
    aliases = (
        ModelAliasReconciler(lambda: SqlUnitOfWork(sessions), experiments)
        if experiments is not None
        else None
    )
    deployments = DeploymentReconciler(lambda: SqlUnitOfWork(sessions), KServeServingProvider(api))
    rollouts = (
        RolloutReconciler(
            lambda: SqlUnitOfWork(sessions),
            KServeServingProvider(api),
            PrometheusMetricsProvider(settings.prometheus_url),
        )
        if settings.prometheus_url
        else None
    )
    if rollouts is None:
        log.warning("CP_PROMETHEUS_URL is not set: canary rollouts will not be driven")
    while True:
        # Only report passes that did something; converged projects are silent.
        _pass(
            "projects",
            lambda: [r for r in projects.reconcile_all() if r.before != r.after or r.changed],
        )
        _pass("runs", lambda: [r for r in runs.reconcile_all() if r.before != r.after])
        _pass(
            "pipeline_runs",
            lambda: [r for r in pipeline_runs.reconcile_all() if r.before != r.after],
        )
        _pass(
            "deployments",
            lambda: [r for r in deployments.reconcile_all() if r.before != r.after or r.applied],
        )
        if rollouts is not None:
            _pass(
                "rollouts",
                lambda: [r for r in rollouts.reconcile_all() if r.before != r.after or r.percent],
            )
        if aliases is not None:
            _pass(
                "model_aliases",
                lambda: [r for r in aliases.reconcile_all() if r.synced or r.drift],
            )
        time.sleep(settings.reconcile_interval_seconds)


if __name__ == "__main__":
    main()
