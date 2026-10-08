from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from controlplane.application.batch_snapshots import resolve_batch_snapshot
from controlplane.application.context import current_traceparent
from controlplane.application.identity import current_actor
from controlplane.application.jobs import resolve_project
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.application.providers import ExperimentProvider, ExperimentRun
from controlplane.application.workflow_compiler import (
    TAG_PIPELINE_RUN_ID,
    TAG_STEP,
    tracking_experiment_name,
)
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import PipelineDefinition, PipelineRun, StepRun, validate_timeout
from controlplane.domain.errors import AlreadyExists, Conflict, NotFound
from controlplane.domain.parameters import fingerprint, project_values, resolve
from controlplane.domain.states import ProjectStatus, RunStatus, StepStatus


@dataclass(frozen=True, slots=True)
class PipelineRunView:
    run: PipelineRun
    definition: PipelineDefinition
    steps: Sequence[StepRun]  # in pipeline execution order


@dataclass(frozen=True, slots=True)
class TrackedRun:
    step: str | None
    run: ExperimentRun


def _audit(
    entity_type: str,
    entity_id: UUID,
    project_id: UUID,
    action: str,
    now: datetime,
    **payload: object,
) -> AuditEvent:
    return AuditEvent(
        occurred_at=now,
        actor=current_actor(),
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        project_id=project_id,
        payload=payload,
    )


def ordered_steps(definition: PipelineDefinition, steps: Sequence[StepRun]) -> list[StepRun]:
    by_name = {s.step_name: s for s in steps}
    return [by_name[name] for name in definition.execution_order if name in by_name]


class PipelineRunService:
    """Records intent (a run and its steps). The PipelineRunReconciler executes it."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock = utc_now,
        experiments: ExperimentProvider | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._experiments = experiments

    def create(
        self,
        project_ref: str,
        pipeline_name: str,
        *,
        version: int | None = None,
        commit_sha: str | None = None,
        idempotency_key: str | None = None,
        timeout_seconds: int = 3600,
        parameters: Mapping[str, Any] | None = None,
    ) -> tuple[PipelineRunView, bool]:
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            requested = uow.pipelines.get_version(project.id, pipeline_name, version)
            if requested is None:
                raise NotFound("pipeline", pipeline_name)
            parameters = resolve(requested.parameter_schema, parameters)
            version = requested.version  # freeze latest before any unique-key race
        try:
            return self._create(
                project_ref,
                pipeline_name,
                version,
                commit_sha,
                idempotency_key,
                validate_timeout(timeout_seconds),
                parameters,
            )
        except AlreadyExists:  # lost a race on the idempotency key: replay the winner
            with self._uow_factory() as uow:
                project = resolve_project(uow, project_ref)
                assert idempotency_key is not None
                existing = uow.pipeline_runs.get_by_idempotency_key(project.id, idempotency_key)
            if existing is None:
                raise
            if existing.pipeline_definition_id != requested.id or existing.commit_sha != commit_sha:
                raise Conflict(
                    "idempotency key was used for a different pipeline or commit"
                ) from None
            if existing.timeout_seconds != timeout_seconds:
                raise Conflict("idempotency key was used with a different timeout") from None
            if existing.parameters != parameters:
                raise Conflict("idempotency key was used with different parameters") from None
            return self.view(existing.id), False

    def _create(
        self,
        project_ref: str,
        pipeline_name: str,
        version: int | None,
        commit_sha: str | None,
        key: str | None,
        timeout_seconds: int,
        parameters: Mapping[str, Any] | None = None,
    ) -> tuple[PipelineRunView, bool]:
        with self._uow_factory() as uow:
            result = self.create_in_uow(
                uow,
                project_ref,
                pipeline_name,
                version,
                commit_sha,
                key,
                timeout_seconds,
                parameters,
            )
            uow.commit()
            return result

    def create_in_uow(
        self,
        uow: UnitOfWork,
        project_ref: str,
        pipeline_name: str,
        version: int | None,
        commit_sha: str | None,
        key: str | None,
        timeout_seconds: int,
        parameters: Mapping[str, Any] | None = None,
    ) -> tuple[PipelineRunView, bool]:
        project = resolve_project(uow, project_ref)
        definition = uow.pipelines.get_version(project.id, pipeline_name, version)
        if definition is None:
            raise NotFound(
                "pipeline", pipeline_name if version is None else f"{pipeline_name}@{version}"
            )
        resolved = resolve(definition.parameter_schema, parameters)
        jobs = {}
        for step in definition.steps:
            job = uow.jobs.get_by_name(project.id, step.job)
            if job is None:
                raise NotFound("job", step.job)
            project_values(job.parameter_schema, resolved)
            jobs[step.name] = job
        if key is not None:
            existing = uow.pipeline_runs.get_by_idempotency_key(project.id, key)
            if existing is not None:
                if existing.pipeline_definition_id != definition.id:
                    raise Conflict(f"idempotency key {key!r} was used for a different pipeline")
                if existing.commit_sha != commit_sha:
                    raise Conflict("idempotency key was used with a different commit")
                if existing.timeout_seconds != timeout_seconds:
                    raise Conflict("idempotency key was used with a different timeout")
                if existing.parameters != resolved:
                    raise Conflict("idempotency key was used with different parameters")
                return self._view(uow, existing), False
        if project.status is not ProjectStatus.READY:
            raise Conflict(
                f"project {project.name!r} is {project.status.value}; runs need a READY project"
            )
        snapshots = {
            name: resolve_batch_snapshot(uow, job, project_values(job.parameter_schema, resolved))
            for name, job in jobs.items()
            if job.batch_spec
        }
        if len(json.dumps(snapshots).encode()) > 1048576:
            raise Conflict("pipeline batch execution snapshots exceed size limit")
        now = self._clock()
        run = PipelineRun(
            project_id=project.id,
            pipeline_definition_id=definition.id,
            parameters=resolved,
            batch_snapshots=snapshots,
            commit_sha=commit_sha,
            timeout_seconds=timeout_seconds,
            idempotency_key=key,
            traceparent=current_traceparent(),
            created_at=now,
            updated_at=now,
        )
        uow.pipeline_runs.add(run)
        uow.step_runs.add_many(
            [
                StepRun(pipeline_run_id=run.id, step_name=name, created_at=now, updated_at=now)
                for name in definition.execution_order
            ]
        )
        uow.audit.record(
            _audit(
                "pipeline_run",
                run.id,
                project.id,
                "pipeline_run.created",
                now,
                pipeline=definition.name,
                version=definition.version,
                parameters_sha256=fingerprint(resolved),
                input_datasets={
                    name: value["input_dataset_id"] for name, value in snapshots.items()
                },
            )
        )
        return self._view(uow, run), True

    @staticmethod
    def _view(uow: UnitOfWork, run: PipelineRun) -> PipelineRunView:
        definition = uow.pipelines.get(run.pipeline_definition_id)
        assert definition is not None
        return PipelineRunView(
            run, definition, ordered_steps(definition, uow.step_runs.list(run.id))
        )

    def view(self, run_id: UUID) -> PipelineRunView:
        with self._uow_factory() as uow:
            run = uow.pipeline_runs.get(run_id)
            if run is None:
                raise NotFound("pipeline run", run_id)
            return self._view(uow, run)

    def list(
        self,
        project_ref: str,
        *,
        pipeline_name: str | None = None,
        limit: int = 50,
        offset: int = 0,
        statuses: Collection[RunStatus] | None = None,
    ) -> Sequence[PipelineRun]:
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            ids = None
            if pipeline_name is not None:
                latest = uow.pipelines.get_version(project.id, pipeline_name, None)
                if latest is None:
                    raise NotFound("pipeline", pipeline_name)
                ids = [
                    d.id
                    for v in range(1, latest.version + 1)
                    if (d := uow.pipelines.get_version(project.id, pipeline_name, v)) is not None
                ]
            return uow.pipeline_runs.list(
                project.id, definition_ids=ids, limit=limit, offset=offset, statuses=statuses
            )

    def definition_labels(self, runs: Sequence[PipelineRun]) -> dict[UUID, tuple[str, int]]:
        """pipeline definition id -> (name, version) for the runs given."""
        with self._uow_factory() as uow:
            labels = {}
            for definition_id in {r.pipeline_definition_id for r in runs}:
                definition = uow.pipelines.get(definition_id)
                if definition is not None:
                    labels[definition_id] = (definition.name, definition.version)
            return labels

    def request_cancel(self, run_id: UUID) -> PipelineRunView:
        """Idempotent. A PENDING run has no workload: it and its steps are cancelled at
        once. A submitted run is flagged and the reconciler stops the workload."""
        with self._uow_factory() as uow:
            run = uow.pipeline_runs.get(run_id)
            if run is None:
                raise NotFound("pipeline run", run_id)
            if run.is_terminal or run.cancel_requested:
                return self._view(uow, run)
            now = self._clock()
            if run.status is RunStatus.PENDING:
                updated = run.transition_to(
                    RunStatus.CANCELLED, now, reason="cancelled before submission"
                )
                for step in uow.step_runs.list(run.id):
                    uow.step_runs.update(
                        step.transition_to(StepStatus.CANCELLED, now),
                        expected_status=step.status,
                    )
            else:
                updated = run.with_cancel_requested(now)
            uow.pipeline_runs.update(updated, expected_status=run.status)
            uow.audit.record(
                _audit(
                    "pipeline_run",
                    run.id,
                    run.project_id,
                    "pipeline_run.cancel_requested",
                    now,
                    status=updated.status.value,
                )
            )
            uow.commit()
            return self._view(uow, updated)

    def tracking(self, run_id: UUID) -> Sequence[TrackedRun]:
        """The tracker runs (params, metrics, artifacts) produced by this pipeline run,
        found by platform tags, so callers never need a tracker id."""
        if self._experiments is None:
            raise Conflict("no experiment tracking provider is configured")
        with self._uow_factory() as uow:
            run = uow.pipeline_runs.get(run_id)
            if run is None:
                raise NotFound("pipeline run", run_id)
            project = uow.projects.get(run.project_id)
        assert project is not None
        experiment = self._experiments.ensure_experiment(
            project.id, tracking_experiment_name(project)
        )
        found = self._experiments.find_runs(experiment, {TAG_PIPELINE_RUN_ID: str(run_id)})
        return [TrackedRun(step=r.tags.get(TAG_STEP), run=r) for r in found]
