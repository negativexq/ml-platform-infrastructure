"""Bounded cleanup of external workflows; keep platform history and lineage."""

from dataclasses import replace
from datetime import timedelta
from functools import partial
from uuid import UUID

from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.application.providers import WorkflowProvider
from controlplane.domain.audit import AuditEvent
from controlplane.reconciliation.batch import ReconcileBackoff, reconcile_batch


class WorkflowRetentionReconciler:
    def __init__(
        self,
        uow: UnitOfWorkFactory,
        workflow: WorkflowProvider,
        keep_seconds: int = 0,
        clock: Clock = utc_now,
    ) -> None:
        if keep_seconds < 0:
            raise ValueError("retention must be nonnegative")
        self._uow = uow
        self._workflow = workflow
        self._keep = keep_seconds
        self._clock = clock
        self._retry = {"runs": ReconcileBackoff(), "pipeline_runs": ReconcileBackoff()}

    def reconcile_all(self) -> list[UUID]:
        if not self._keep:
            return []
        before = self._clock() - timedelta(seconds=self._keep)
        cleaned: list[UUID] = []
        for name in ("runs", "pipeline_runs"):
            with self._uow() as uow:
                ids = [r.id for r in getattr(uow, name).list_cleanup_candidates(before, 100)]
            cleaned.extend(
                reconcile_batch(
                    ids,
                    partial(self._clean, name),
                    "workflow_retention." + name,
                    self._retry[name],
                )
            )
        return cleaned

    def _clean(self, name: str, entity_id: UUID) -> UUID:
        with self._uow() as uow:
            run = getattr(uow, name).get(entity_id)
        now = self._clock()
        if (
            run is None
            or not run.is_terminal
            or run.external_ref is None
            or run.workflow_cleaned_at is not None
            or run.finished_at is None
            or run.finished_at >= now - timedelta(seconds=self._keep)
        ):
            return entity_id
        # A crash after delete is safe: the provider treats 404 as success.
        self._workflow.delete(run.external_ref)
        with self._uow() as uow:
            current = getattr(uow, name).get(entity_id)
            if current.workflow_cleaned_at is None:
                getattr(uow, name).update(
                    replace(current, workflow_cleaned_at=now, updated_at=now),
                    expected_status=run.status,
                )
                uow.audit.record(
                    AuditEvent(
                        occurred_at=now,
                        actor="reconciler",
                        action="workflow.cleaned",
                        entity_type="run" if name == "runs" else "pipeline_run",
                        entity_id=entity_id,
                        project_id=run.project_id,
                        payload={"retention_seconds": self._keep},
                    )
                )
                uow.commit()
        return entity_id
