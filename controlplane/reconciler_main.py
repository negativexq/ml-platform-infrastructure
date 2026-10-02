"""Reconciler entrypoint: `python -m controlplane.reconciler_main`.

Runs the project and run reconcilers in one loop. Each pass is independent: a
failure in one reconciler is logged and does not stop the other.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from controlplane.adapters.kubernetes import KubernetesClusterProvider, load_api_client
from controlplane.adapters.workflow import ArgoWorkflowProvider
from controlplane.persistence.sql import SqlUnitOfWork, make_engine, sql_uow_factory
from controlplane.reconciliation.projects import ProjectReconciler
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
    while True:
        # Only report passes that did something; converged projects are silent.
        _pass(
            "projects",
            lambda: [r for r in projects.reconcile_all() if r.before != r.after or r.changed],
        )
        _pass("runs", lambda: [r for r in runs.reconcile_all() if r.before != r.after])
        time.sleep(settings.reconcile_interval_seconds)


if __name__ == "__main__":
    main()
