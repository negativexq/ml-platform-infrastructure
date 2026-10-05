"""Retry scoped registry discovery after successful pipelines, with durable checkpoints."""

from datetime import timedelta
from uuid import UUID

from controlplane.application.models import ModelService
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.application.providers import ExperimentProvider
from controlplane.domain.audit import AuditEvent
from controlplane.domain.states import ModelKind, RunStatus
from controlplane.reconciliation.batch import ReconcileBackoff, reconcile_batch


class ModelDiscoveryReconciler:
    def __init__(
        self,
        uow: UnitOfWorkFactory,
        experiments: ExperimentProvider,
        clock: Clock = utc_now,
        *,
        delay_seconds: int = 120,
    ) -> None:
        self._uow, self._clock = uow, clock
        self._models = ModelService(uow, clock, experiments)
        self._delay = timedelta(seconds=delay_seconds)
        self._backoff = ReconcileBackoff()

    def reconcile_all(self) -> list[int]:
        with self._uow() as uow:
            ids = [
                r.id
                for r in uow.pipeline_runs.list_discovery_candidates(
                    self._clock() - self._delay, 100
                )
            ]
        return reconcile_batch(ids, self.reconcile, "model_discovery", self._backoff)

    def reconcile(self, run_id: UUID) -> int:
        with self._uow() as uow:
            run = uow.pipeline_runs.get(run_id)
            if run is None or run.status is not RunStatus.SUCCEEDED or run.models_discovered_at:
                return 0
            project = uow.projects.get(run.project_id)
            if project is None:
                return 0
            models = [m for m in uow.models.list(project.id) if m.kind is ModelKind.CLASSIC]
        created = seen = 0
        for model in models:
            result = self._models.discover(project.name, model.name, pipeline_run_id=run.id)
            created += len(result.created)
            seen += len(result.created) + result.existing
        now = self._clock()
        with self._uow() as uow:
            current = uow.pipeline_runs.get(run_id)
            assert current is not None
            if current.models_discovered_at is not None:
                return 0
            if not uow.pipeline_runs.mark_model_discovery(run_id, now, bool(seen)):
                return 0
            if seen:
                uow.audit.record(
                    AuditEvent(
                        occurred_at=now,
                        actor="reconciler",
                        action="pipeline.models_discovered",
                        entity_type="pipeline_run",
                        entity_id=run_id,
                        project_id=project.id,
                        payload={"created": created, "seen": seen},
                    )
                )
            uow.commit()
        return created
