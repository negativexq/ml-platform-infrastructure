"""Reconciler entrypoint: `python -m controlplane.reconciler_main`."""

from __future__ import annotations

import logging
import time

from controlplane.adapters.kubernetes import KubernetesClusterProvider
from controlplane.persistence.sql import SqlUnitOfWork, make_engine, sql_uow_factory
from controlplane.reconciliation.projects import ProjectReconciler
from controlplane.settings import Settings

log = logging.getLogger("controlplane.reconciler")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    sessions = sql_uow_factory(make_engine(settings.database_url))
    reconciler = ProjectReconciler(
        lambda: SqlUnitOfWork(sessions),
        KubernetesClusterProvider.from_kubeconfig(settings.kubeconfig or None),
    )
    while True:
        try:
            for result in reconciler.reconcile_all():
                if result.before != result.after or result.changed:
                    log.info(
                        "project %s: %s -> %s changed=%s",
                        result.project_id,
                        result.before.value,
                        result.after.value,
                        list(result.changed),
                    )
        except Exception:  # noqa: BLE001 - one bad pass must not kill the loop
            log.exception("reconcile pass failed")
        time.sleep(settings.reconcile_interval_seconds)


if __name__ == "__main__":
    main()
