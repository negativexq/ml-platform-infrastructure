"""Executes pipeline runs and mirrors their per-step progress into the database.

One pipeline run is one workflow (a DAG). Each pass copies what the workflow
system reports onto the run and onto every StepRun, so the platform's own tables
always answer "what happened to step X" without asking Argo.

    PipelineRun: PENDING -> SUBMITTED -> RUNNING -> SUCCEEDED | FAILED | CANCELLED
    StepRun:     PENDING -> RUNNING -> SUCCEEDED | FAILED | CANCELLED
                 PENDING -> SKIPPED      (an upstream step did not succeed)

A step the workflow never ran is SKIPPED, never left PENDING under a finished run.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.application.providers import (
    ExperimentProvider,
    ExternalState,
    WorkflowProvider,
    WorkflowStatus,
)
from controlplane.application.workflow_compiler import (
    compile_pipeline_run,
    tracking_experiment_name,
)
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import PipelineRun, StepRun
from controlplane.domain.errors import Conflict, NotFound
from controlplane.domain.states import RunStatus, StepStatus

SYSTEM = "reconciler"

_RUN_STATE = {
    ExternalState.RUNNING: RunStatus.RUNNING,
    ExternalState.SUCCEEDED: RunStatus.SUCCEEDED,
    ExternalState.FAILED: RunStatus.FAILED,
    ExternalState.CANCELLED: RunStatus.CANCELLED,
}
_STEP_STATE = {
    ExternalState.RUNNING: StepStatus.RUNNING,
    ExternalState.SUCCEEDED: StepStatus.SUCCEEDED,
    ExternalState.FAILED: StepStatus.FAILED,
    ExternalState.CANCELLED: StepStatus.CANCELLED,
    ExternalState.SKIPPED: StepStatus.SKIPPED,
}


@dataclass(frozen=True, slots=True)
class PipelineRunResult:
    run_id: UUID
    before: RunStatus
    after: RunStatus


class PipelineRunReconciler:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        workflow: WorkflowProvider,
        experiments: ExperimentProvider | None = None,
        tracking_uri: str | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._uow_factory = uow_factory
        self._workflow = workflow
        self._experiments = experiments
        self._tracking_uri = tracking_uri
        self._clock = clock

    def reconcile_all(self) -> list[PipelineRunResult]:
        with self._uow_factory() as uow:
            ids = [r.id for r in uow.pipeline_runs.list_active()]
        results = []
        for run_id in ids:
            try:
                results.append(self.reconcile(run_id))
            except Conflict:
                continue  # another writer moved it first; next pass picks it up
        return results

    def reconcile(self, run_id: UUID) -> PipelineRunResult:
        run = self._load(run_id)
        before = run.status
        if run.is_terminal:
            return PipelineRunResult(run_id, before, before)
        if run.status is RunStatus.PENDING:
            run = self._submit(run)
        if run.status in (RunStatus.SUBMITTED, RunStatus.RUNNING):
            run = self._sync(run)
        return PipelineRunResult(run_id, before, run.status)

    # -- PENDING -> SUBMITTED ---------------------------------------------

    def _submit(self, run: PipelineRun) -> PipelineRun:
        if run.cancel_requested:
            return self._finish(run, RunStatus.CANCELLED, "cancelled")
        with self._uow_factory() as uow:
            project = uow.projects.get(run.project_id)
            definition = uow.pipelines.get(run.pipeline_definition_id)
            jobs = {}
            if definition is not None and project is not None:
                for step in definition.steps:
                    job = uow.jobs.get_by_name(project.id, step.job)
                    if job is not None:
                        jobs[step.job] = job
        if (
            project is None
            or definition is None
            or len(jobs) != len({s.job for s in definition.steps})
        ):
            return self._finish(run, RunStatus.FAILED, "project, pipeline or a job is gone")
        try:
            if self._experiments is not None:
                # Create the experiment up front so parallel steps never race to create it.
                self._experiments.ensure_experiment(project.id, tracking_experiment_name(project))
            spec = compile_pipeline_run(
                project, definition, jobs, run, tracking_uri=self._tracking_uri
            )
            ref = self._workflow.submit(spec, str(run.id))
        except Exception as exc:  # noqa: BLE001 - a rejected submission must surface as FAILED
            return self._finish(run, RunStatus.FAILED, f"submission failed: {exc}")
        return self._move_run(run, RunStatus.SUBMITTED, "pipeline_run.submitted", external_ref=ref)

    # -- SUBMITTED/RUNNING -> ... -----------------------------------------

    def _sync(self, run: PipelineRun) -> PipelineRun:
        assert run.external_ref is not None
        if run.cancel_requested:
            self._workflow.cancel(run.external_ref)  # idempotent on the provider side
        try:
            status = self._workflow.get_status(run.external_ref)
        except NotFound:
            return self._finish(run, RunStatus.FAILED, "workload disappeared from the cluster")

        self._sync_steps(run, status)
        target = _RUN_STATE.get(status.state)
        if target is None or target is run.status:
            return run
        if run.status is RunStatus.SUBMITTED and target is RunStatus.SUCCEEDED:
            run = self._move_run(run, RunStatus.RUNNING, "pipeline_run.running")
        if target is RunStatus.RUNNING:
            return self._move_run(run, target, "pipeline_run.running")
        return self._finish(run, target, status.reason or self._failure_reason(run, target))

    def _failure_reason(self, run: PipelineRun, target: RunStatus) -> str | None:
        if target is not RunStatus.FAILED:
            return None
        with self._uow_factory() as uow:
            failed = [
                s.step_name for s in uow.step_runs.list(run.id) if s.status is StepStatus.FAILED
            ]
        return f"failed steps: {', '.join(failed)}" if failed else "workflow failed"

    # -- steps -------------------------------------------------------------

    def _sync_steps(self, run: PipelineRun, status: WorkflowStatus) -> None:
        with self._uow_factory() as uow:
            steps = list(uow.step_runs.list(run.id))
        for step in steps:
            if step.is_terminal:
                continue
            target = _STEP_STATE.get(status.steps.get(step.step_name, ExternalState.PENDING))
            if target is None or target is step.status:
                continue
            self._advance_step(step, target, status.exit_codes.get(step.step_name))

    def _advance_step(self, step: StepRun, target: StepStatus, exit_code: int | None) -> StepRun:
        if step.status is StepStatus.PENDING and target in (
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
        ):
            step = self._move_step(step, StepStatus.RUNNING)  # never PENDING -> terminal directly
        return self._move_step(step, target, exit_code=exit_code)

    def _close_open_steps(self, run: PipelineRun, run_status: RunStatus) -> None:
        """A finished run leaves no step PENDING or RUNNING."""
        closing = StepStatus.CANCELLED if run_status is RunStatus.CANCELLED else StepStatus.SKIPPED
        reason = f"pipeline run {run_status.value.lower()}"
        with self._uow_factory() as uow:
            steps = list(uow.step_runs.list(run.id))
        for step in steps:
            if step.is_terminal:
                continue
            if step.status is StepStatus.RUNNING:
                self._move_step(step, StepStatus.CANCELLED, reason=reason)
            else:
                self._move_step(step, closing, reason=reason)

    # -- persistence -------------------------------------------------------

    def _load(self, run_id: UUID) -> PipelineRun:
        with self._uow_factory() as uow:
            run = uow.pipeline_runs.get(run_id)
        if run is None:
            raise NotFound("pipeline run", run_id)
        return run

    def _finish(self, run: PipelineRun, status: RunStatus, reason: str | None) -> PipelineRun:
        finished = self._move_run(
            run, status, f"pipeline_run.{status.value.lower()}", reason=reason
        )
        self._close_open_steps(finished, status)
        return finished

    def _move_run(
        self,
        run: PipelineRun,
        status: RunStatus,
        action: str,
        *,
        reason: str | None = None,
        external_ref: str | None = None,
    ) -> PipelineRun:
        now = self._clock()
        moved = run.transition_to(status, now, reason=reason, external_ref=external_ref)
        with self._uow_factory() as uow:
            uow.pipeline_runs.update(moved, expected_status=run.status)
            uow.audit.record(
                AuditEvent(
                    occurred_at=now,
                    actor=SYSTEM,
                    action=action,
                    entity_type="pipeline_run",
                    entity_id=run.id,
                    project_id=run.project_id,
                    payload={
                        "from": run.status.value,
                        "to": status.value,
                        "reason": moved.status_reason,
                    },
                )
            )
            uow.commit()
        return moved

    def _move_step(
        self,
        step: StepRun,
        status: StepStatus,
        *,
        reason: str | None = None,
        exit_code: int | None = None,
    ) -> StepRun:
        now = self._clock()
        moved = step.transition_to(status, now, reason=reason, exit_code=exit_code)
        with self._uow_factory() as uow:
            run = uow.pipeline_runs.get(step.pipeline_run_id)
            uow.step_runs.update(moved, expected_status=step.status)
            uow.audit.record(
                AuditEvent(
                    occurred_at=now,
                    actor=SYSTEM,
                    action=f"step_run.{status.value.lower()}",
                    entity_type="step_run",
                    entity_id=step.id,
                    project_id=run.project_id if run else None,
                    payload={
                        "pipeline_run_id": str(step.pipeline_run_id),
                        "step": step.step_name,
                        "from": step.status.value,
                        "to": status.value,
                        "exit_code": moved.exit_code,
                    },
                )
            )
            uow.commit()
        return moved
