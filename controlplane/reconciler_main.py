"""Reconciler entrypoint: `python -m controlplane.reconciler_main`.

Runs every reconciler in one loop. Each pass is independent: a failure in one reconciler is
logged and does not stop the others. Telemetry is configured from OTEL_* (see
`controlplane.observability.setup`); with none set it costs nothing.
"""

from __future__ import annotations

import signal
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

import structlog

from controlplane import observability
from controlplane.adapters.kubernetes import KubernetesClusterProvider, load_api_client
from controlplane.adapters.kubernetes.networking import NetworkTopology
from controlplane.adapters.metrics import PrometheusMetricsProvider
from controlplane.adapters.mlflow import MlflowExperimentProvider
from controlplane.adapters.serving import KServeServingProvider
from controlplane.adapters.workflow import ArgoWorkflowProvider
from controlplane.application.ports import UnitOfWork
from controlplane.observability import instrument_reconciler, observe, observed_uow_factory
from controlplane.persistence.sql import SqlUnitOfWork, make_engine, sql_uow_factory
from controlplane.reconciliation.deployments import DeploymentReconciler
from controlplane.reconciliation.model_aliases import ModelAliasReconciler
from controlplane.reconciliation.model_discovery import ModelDiscoveryReconciler
from controlplane.reconciliation.pipeline_runs import PipelineRunReconciler
from controlplane.reconciliation.projects import ProjectReconciler
from controlplane.reconciliation.retention import WorkflowRetentionReconciler
from controlplane.reconciliation.rollouts import RolloutReconciler
from controlplane.reconciliation.runs import RunReconciler
from controlplane.settings import Settings

SERVICE_NAME = "mlp-controlplane-reconciler"
log = structlog.get_logger("controlplane.reconciler")


def _traceparent(
    getter: Callable[[UnitOfWork], Callable[[UUID], Any]],
) -> Callable[[UnitOfWork, UUID], str | None]:
    """How to read the trace an entity was created in (so its changes join that trace)."""

    def read(uow: UnitOfWork, entity_id: UUID) -> str | None:
        entity = getter(uow)(entity_id)
        return None if entity is None else entity.traceparent

    return read


def _pass(name: str, step: Callable[[], list[Any]]) -> None:
    try:
        for result in step():
            log.info("reconciled", reconciler=name, result=str(result))
    except Exception:  # noqa: BLE001 - one bad pass must not kill the loop
        log.exception("pass failed", reconciler=name)


def main() -> None:
    settings = Settings()
    telemetry = observability.configure(
        SERVICE_NAME, json_logs=settings.log_json, log_level=settings.log_level
    )
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(0)))

    sessions = sql_uow_factory(make_engine(settings.database_url))
    uow = observed_uow_factory(lambda: SqlUnitOfWork(sessions))
    api = load_api_client(settings.kubeconfig or None)

    topology = NetworkTopology(
        system_namespace=settings.system_namespace,
        platform_namespace=settings.platform_namespace,
        observability_namespace=settings.observability_namespace,
        serving_namespaces=tuple(
            s.strip() for s in settings.serving_namespaces.split(",") if s.strip()
        ),
        api_cidrs=tuple(s.strip() for s in settings.cluster_api_cidrs.split(",") if s.strip()),
        external_https_cidrs=tuple(
            s.strip() for s in settings.external_https_cidrs.split(",") if s.strip()
        ),
        isolate_egress=settings.project_egress_enabled,
    )
    cluster = observe(
        KubernetesClusterProvider(api, topology), "cluster", observability.CLUSTER_MUTATIONS
    )
    workflow = observe(ArgoWorkflowProvider(api), "workflow", observability.WORKFLOW_MUTATIONS)
    serving = observe(KServeServingProvider(api), "serving", observability.SERVING_MUTATIONS)
    experiments = (
        observe(
            MlflowExperimentProvider(settings.mlflow_tracking_uri),
            "experiments",
            observability.EXPERIMENT_MUTATIONS,
        )
        if settings.mlflow_tracking_uri
        else None
    )

    def instrumented(name: str, reconciler: Any, getter: Any = None) -> Any:
        read = _traceparent(getter) if getter is not None else None
        return instrument_reconciler(reconciler, name, uow, read)

    projects = instrumented("projects", ProjectReconciler(uow, cluster), lambda u: u.projects.get)
    runs = instrumented("runs", RunReconciler(uow, workflow), lambda u: u.runs.get)
    pipeline_runs = instrumented(
        "pipeline_runs",
        PipelineRunReconciler(
            uow,
            workflow,
            experiments,
            tracking_uri=settings.step_mlflow_tracking_uri or settings.mlflow_tracking_uri or None,
        ),
        lambda u: u.pipeline_runs.get,
    )
    deployments = instrumented(
        "deployments", DeploymentReconciler(uow, serving), lambda u: u.deployments.get
    )
    rollouts = None
    if settings.prometheus_url:
        rollouts = instrumented(
            "rollouts",
            RolloutReconciler(
                uow,
                serving,
                observe(
                    PrometheusMetricsProvider(settings.prometheus_url),
                    "metrics",
                    observability.NO_MUTATIONS,
                ),
            ),
            lambda u: u.rollouts.get,
        )
    else:
        log.warning("CP_PROMETHEUS_URL is not set: canary rollouts will not be driven")
    aliases = (
        instrumented("model_aliases", ModelAliasReconciler(uow, experiments))
        if experiments is not None
        else None
    )

    discovery = (
        ModelDiscoveryReconciler(
            uow, experiments, delay_seconds=settings.model_discovery_delay_seconds
        )
        if experiments is not None
        else None
    )
    retention = WorkflowRetentionReconciler(uow, workflow, settings.workflow_retention_seconds)

    log.info("reconciler started", telemetry=telemetry.enabled)
    try:
        while True:
            # Only report passes that did something; converged entities are silent.
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
                lambda: [
                    r for r in deployments.reconcile_all() if r.before != r.after or r.applied
                ],
            )
            if rollouts is not None:
                _pass(
                    "rollouts",
                    lambda: [
                        r for r in rollouts.reconcile_all() if r.before != r.after or r.percent
                    ],
                )
            if aliases is not None:
                _pass(
                    "model_aliases",
                    lambda: [r for r in aliases.reconcile_all() if r.synced or r.drift],
                )
            if discovery is not None:
                _pass("model_discovery", discovery.reconcile_all)
            if settings.workflow_retention_seconds:
                _pass("workflow_retention", retention.reconcile_all)
            time.sleep(settings.reconcile_interval_seconds)
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    main()
