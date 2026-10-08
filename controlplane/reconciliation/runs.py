"""Moves runs from "recorded in the database" to "finished", by submitting the
workload and mirroring what the workflow system reports.

    PENDING -> SUBMITTED -> RUNNING -> SUCCEEDED | FAILED | CANCELLED

Every transition is a compare-and-swap with its audit event in one transaction.
A crash between "workload submitted" and "SUBMITTED written" is safe: the next
pass re-submits with the same idempotency key and gets the same workload back.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from controlplane.application.batch_inference import publish_output
from controlplane.application.model_monitoring import publish_report
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.application.providers import ExternalState, WorkflowProvider, WorkflowStatus
from controlplane.application.workflow_compiler import MAIN_STEP, compile_job_run
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import Run
from controlplane.domain.errors import InvalidArgument, NotFound
from controlplane.domain.states import RunStatus
from controlplane.reconciliation.batch import ReconcileBackoff, reconcile_batch

SYSTEM = "reconciler"

_PLATFORM_STATE = {
    ExternalState.RUNNING: RunStatus.RUNNING,
    ExternalState.SUCCEEDED: RunStatus.SUCCEEDED,
    ExternalState.FAILED: RunStatus.FAILED,
    ExternalState.CANCELLED: RunStatus.CANCELLED,
}


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: UUID
    before: RunStatus
    after: RunStatus


class RunReconciler:
    def __init__(
        self, uow_factory: UnitOfWorkFactory, workflow: WorkflowProvider, clock: Clock = utc_now
    ) -> None:
        self._uow_factory = uow_factory
        self._workflow = workflow
        self._clock = clock
        self._retry = ReconcileBackoff()

    def reconcile_all(self) -> list[RunResult]:
        with self._uow_factory() as uow:
            ids = [r.id for r in uow.runs.list_active()]
        return reconcile_batch(ids, self.reconcile, "runs", self._retry)

    def reconcile(self, run_id: UUID) -> RunResult:
        run = self._load(run_id)
        before = run.status
        if run.is_terminal:
            return RunResult(run_id, before, before)
        if run.status is RunStatus.PENDING:
            run = self._submit(run)
        if run.status in (RunStatus.SUBMITTED, RunStatus.RUNNING):
            run = self._sync(run)
        return RunResult(run_id, before, run.status)

    # -- PENDING -> SUBMITTED ---------------------------------------------

    def _submit(self, run: Run) -> Run:
        if run.cancel_requested:
            return self._move(run, RunStatus.CANCELLED, "run.cancelled", reason="cancelled")
        with self._uow_factory() as uow:
            project = uow.projects.get(run.project_id)
            job = uow.jobs.get(run.job_definition_id)
        if project is None or job is None:
            return self._move(run, RunStatus.FAILED, "run.failed", reason="project or job is gone")
        try:
            ref = self._workflow.submit(compile_job_run(project, job, run), str(run.id))
        except Exception as exc:  # noqa: BLE001 - a rejected submission must surface as FAILED
            return self._move(
                run, RunStatus.FAILED, "run.failed", reason=f"submission failed: {exc}"
            )
        return self._move(run, RunStatus.SUBMITTED, "run.submitted", external_ref=ref)

    # -- SUBMITTED/RUNNING -> ... -----------------------------------------

    def _sync(self, run: Run) -> Run:
        assert run.external_ref is not None
        if run.cancel_requested:
            self._workflow.cancel(run.external_ref)  # idempotent on the provider side
        try:
            status = self._workflow.get_status(run.external_ref)
        except NotFound:
            return self._move(
                run, RunStatus.FAILED, "run.failed", reason="workload disappeared from the cluster"
            )
        target = _PLATFORM_STATE.get(status.state)
        if target is None or target is run.status:
            return run  # still pending/running: nothing to record
        if run.status is RunStatus.SUBMITTED and target is RunStatus.SUCCEEDED:
            run = self._move(
                run, RunStatus.RUNNING, "run.running"
            )  # fast job: pass through RUNNING
        return self._move(
            run,
            target,
            f"run.{target.value.lower()}",
            reason=status.reason,
            exit_code=_exit_code(status),
            result=status.results.get(MAIN_STEP),
        )

    # -- persistence ------------------------------------------------------

    def _load(self, run_id: UUID) -> Run:
        with self._uow_factory() as uow:
            run = uow.runs.get(run_id)
        if run is None:
            raise NotFound("run", run_id)
        return run

    def _move(
        self,
        run: Run,
        status: RunStatus,
        action: str,
        *,
        reason: str | None = None,
        exit_code: int | None = None,
        external_ref: str | None = None,
        result: str | None = None,
    ) -> Run:
        now = self._clock()
        moved = run.transition_to(
            status, now, reason=reason, exit_code=exit_code, external_ref=external_ref
        )
        with self._uow_factory() as uow:
            if status is RunStatus.SUCCEEDED:
                job = uow.jobs.get(run.job_definition_id)
                if job and job.batch_spec:
                    try:
                        publish_output(uow, job, result, run_id=run.id, now=now, snapshot=run.batch_snapshot)
                    except InvalidArgument:
                        status, action = RunStatus.FAILED, "run.failed"
                        moved = run.transition_to(
                            status,
                            now,
                            reason="invalid or missing batch output result",
                            exit_code=exit_code,
                        )
                if job and job.monitoring_spec:
                    try:
                        publish_report(uow, job, result, run_id=run.id, now=now)
                    except InvalidArgument:
                        status, action = RunStatus.FAILED, "run.failed"
                        moved = run.transition_to(
                            status,
                            now,
                            reason="invalid or missing monitoring result",
                            exit_code=exit_code,
                        )
            uow.runs.update(moved, expected_status=run.status)
            uow.audit.record(
                AuditEvent(
                    occurred_at=now,
                    actor=SYSTEM,
                    action=action,
                    entity_type="run",
                    entity_id=run.id,
                    project_id=run.project_id,
                    payload={
                        "from": run.status.value,
                        "to": status.value,
                        "reason": moved.status_reason,
                        "exit_code": moved.exit_code,
                    },
                )
            )
            uow.commit()
        return moved


def _exit_code(status: WorkflowStatus) -> int | None:
    return status.exit_codes.get(MAIN_STEP)
