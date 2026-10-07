from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import Select

from controlplane.domain.access import Membership, ProjectRole
from controlplane.domain.api_keys import ApiKey
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import (
    Check,
    Deployment,
    DeploymentRevision,
    Endpoint,
    EndpointLimits,
    Evaluation,
    FunctionServing,
    JobDefinition,
    LlmServing,
    Model,
    ModelVersion,
    PipelineDefinition,
    PipelineRun,
    Project,
    Promotion,
    Rollout,
    RolloutGate,
    Run,
    StepRun,
    StepSpec,
    Threshold,
)
from controlplane.domain.errors import AlreadyExists, Conflict, NotFound
from controlplane.domain.secrets import SecretRefs
from controlplane.domain.states import (
    DeploymentStatus,
    EndpointKind,
    EndpointProtocol,
    EndpointStatus,
    EvaluationStatus,
    Exposure,
    ModelKind,
    ModelStatus,
    ProjectStatus,
    PromotionStatus,
    RolloutStatus,
    RunStatus,
    ServingRuntime,
    StepStatus,
)
from controlplane.persistence.engine import make_engine as make_engine
from controlplane.persistence.models import (
    ApiKeyRow,
    AuditEventRow,
    DeploymentRevisionRow,
    DeploymentRow,
    EndpointRow,
    EvaluationRow,
    JobDefinitionRow,
    MembershipRow,
    ModelRow,
    ModelVersionRow,
    NotificationReadRow,
    PipelineDefinitionRow,
    PipelineRunRow,
    ProjectRow,
    PromotionRow,
    RolloutRow,
    RunRow,
    StepRunRow,
)

_UNIQUE_VIOLATION = "23505"


def _project(row: ProjectRow) -> Project:
    return Project(
        id=row.id,
        name=row.name,
        display_name=row.display_name,
        description=row.description,
        status=ProjectStatus(row.status),
        status_reason=row.status_reason,
        traceparent=row.traceparent,
        gpu_quota=row.gpu_quota,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _audit(row: AuditEventRow) -> AuditEvent:
    return AuditEvent(
        id=row.id,
        occurred_at=row.occurred_at,
        actor=row.actor,
        action=row.action,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        project_id=row.project_id,
        payload=row.payload,
        trace_id=row.trace_id,
    )


class SqlProjects:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, project: Project) -> None:
        self._s.add(
            ProjectRow(
                id=project.id,
                name=project.name,
                display_name=project.display_name,
                description=project.description,
                status=project.status.value,
                status_reason=project.status_reason,
                traceparent=project.traceparent,
                gpu_quota=project.gpu_quota,
                created_at=project.created_at,
                updated_at=project.updated_at,
            )
        )
        try:
            self._s.flush()
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION:
                raise AlreadyExists("project", project.name) from exc
            raise

    def get(self, project_id: UUID) -> Project | None:
        row = self._s.get(ProjectRow, project_id)
        return _project(row) if row else None

    def lock(self, project_id: UUID) -> Project | None:
        row = self._s.scalars(
            select(ProjectRow)
            .where(ProjectRow.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        return _project(row) if row else None

    def get_by_name(self, name: str) -> Project | None:
        row = self._s.scalars(select(ProjectRow).where(ProjectRow.name == name)).first()
        return _project(row) if row else None

    def _live(self) -> Select[Any]:
        return (
            select(ProjectRow)
            .where(ProjectRow.status != ProjectStatus.DELETED.value)
            .order_by(ProjectRow.created_at, ProjectRow.id)
        )

    def list(self, *, limit: int, offset: int) -> Sequence[Project]:
        rows = self._s.scalars(self._live().limit(limit).offset(offset))
        return [_project(r) for r in rows]

    def list_reconcilable(self) -> Sequence[Project]:
        return [_project(r) for r in self._s.scalars(self._live())]

    def update(self, project: Project, *, expected_status: ProjectStatus) -> None:
        result = self._s.execute(
            update(ProjectRow)
            .where(ProjectRow.id == project.id, ProjectRow.status == expected_status.value)
            .values(
                status=project.status.value,
                status_reason=project.status_reason,
                gpu_quota=project.gpu_quota,
                updated_at=project.updated_at,
            )
        )
        if getattr(result, "rowcount", 0) == 1:
            return
        if self._s.get(ProjectRow, project.id) is None:
            raise NotFound("project", project.id)
        raise Conflict(f"project {project.id} is no longer {expected_status.value}")


def _job(row: JobDefinitionRow) -> JobDefinition:
    return JobDefinition(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        image=row.image,
        command=tuple(row.command),
        resources=dict(row.resources),
        env=dict(row.env),
        timeout_seconds=row.timeout_seconds,
        secret_refs=SecretRefs.from_json(row.secret_refs),
        parameter_schema=dict(row.parameter_schema),
        created_at=row.created_at,
    )


def _run(row: RunRow) -> Run:
    return Run(
        id=row.id,
        project_id=row.project_id,
        job_definition_id=row.job_definition_id,
        status=RunStatus(row.status),
        status_reason=row.status_reason,
        exit_code=row.exit_code,
        external_ref=row.external_ref,
        cancel_requested=row.cancel_requested,
        retry_of=row.retry_of,
        idempotency_key=row.idempotency_key,
        traceparent=row.traceparent,
        timeout_seconds=row.timeout_seconds,
        parameters=dict(row.parameters),
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        workflow_cleaned_at=row.workflow_cleaned_at,
    )


def _flush_unique(session: Session, entity: str, key: object) -> None:
    try:
        session.flush()
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION:
            raise AlreadyExists(entity, key) from exc
        raise


class SqlJobs:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, job: JobDefinition) -> None:
        self._s.add(
            JobDefinitionRow(
                id=job.id,
                project_id=job.project_id,
                name=job.name,
                image=job.image,
                command=list(job.command),
                resources=dict(job.resources),
                env=dict(job.env),
                timeout_seconds=job.timeout_seconds,
                secret_refs=job.secret_refs.to_json(),
                parameter_schema=dict(job.parameter_schema),
                created_at=job.created_at,
            )
        )
        _flush_unique(self._s, "job", job.name)

    def get(self, job_id: UUID) -> JobDefinition | None:
        row = self._s.get(JobDefinitionRow, job_id)
        return _job(row) if row else None

    def get_by_name(self, project_id: UUID, name: str) -> JobDefinition | None:
        row = self._s.scalars(
            select(JobDefinitionRow).where(
                JobDefinitionRow.project_id == project_id, JobDefinitionRow.name == name
            )
        ).first()
        return _job(row) if row else None

    def list(self, project_id: UUID) -> Sequence[JobDefinition]:
        rows = self._s.scalars(
            select(JobDefinitionRow)
            .where(JobDefinitionRow.project_id == project_id)
            .order_by(JobDefinitionRow.created_at, JobDefinitionRow.id)
        )
        return [_job(r) for r in rows]


class SqlRuns:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, run: Run) -> None:
        self._s.add(
            RunRow(
                id=run.id,
                project_id=run.project_id,
                job_definition_id=run.job_definition_id,
                status=run.status.value,
                status_reason=run.status_reason,
                exit_code=run.exit_code,
                external_ref=run.external_ref,
                cancel_requested=run.cancel_requested,
                retry_of=run.retry_of,
                idempotency_key=run.idempotency_key,
                traceparent=run.traceparent,
                timeout_seconds=run.timeout_seconds,
                parameters=dict(run.parameters),
                created_at=run.created_at,
                updated_at=run.updated_at,
                started_at=run.started_at,
                finished_at=run.finished_at,
                workflow_cleaned_at=run.workflow_cleaned_at,
            )
        )
        _flush_unique(self._s, "run", run.idempotency_key)

    def get(self, run_id: UUID) -> Run | None:
        row = self._s.get(RunRow, run_id)
        return _run(row) if row else None

    def get_by_idempotency_key(self, project_id: UUID, key: str) -> Run | None:
        row = self._s.scalars(
            select(RunRow).where(RunRow.project_id == project_id, RunRow.idempotency_key == key)
        ).first()
        return _run(row) if row else None

    def list(
        self,
        project_id: UUID,
        *,
        job_id: UUID | None,
        limit: int,
        offset: int,
        statuses: Collection[RunStatus] | None = None,
        finished_since: datetime | None = None,
    ) -> Sequence[Run]:
        stmt = select(RunRow).where(RunRow.project_id == project_id)
        if finished_since is not None:
            stmt = stmt.where(
                func.coalesce(RunRow.finished_at, RunRow.updated_at) >= finished_since
            )
        if statuses:
            stmt = stmt.where(RunRow.status.in_([s.value for s in statuses]))
        if job_id is not None:
            stmt = stmt.where(RunRow.job_definition_id == job_id)
        stmt = stmt.order_by(RunRow.created_at.desc(), RunRow.id.desc()).limit(limit).offset(offset)
        return [_run(r) for r in self._s.scalars(stmt)]

    def count(self, project_id: UUID) -> int:
        return int(
            self._s.scalar(
                select(func.count()).select_from(RunRow).where(RunRow.project_id == project_id)
            )
            or 0
        )

    def list_active(self) -> Sequence[Run]:
        terminal = [s.value for s in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)]
        rows = self._s.scalars(
            select(RunRow)
            .where(RunRow.status.not_in(terminal))
            .order_by(RunRow.created_at, RunRow.id)
        )
        return [_run(r) for r in rows]

    def list_cleanup_candidates(self, before: datetime, limit: int) -> Sequence[Run]:
        rows = self._s.scalars(
            select(RunRow)
            .where(
                RunRow.status.in_(
                    [s.value for s in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)]
                ),
                RunRow.finished_at < before,
                RunRow.external_ref.is_not(None),
                RunRow.workflow_cleaned_at.is_(None),
            )
            .order_by(RunRow.finished_at, RunRow.id)
            .limit(limit)
        )
        return [_run(r) for r in rows]

    def update(self, run: Run, *, expected_status: RunStatus) -> None:
        result = self._s.execute(
            update(RunRow)
            .where(RunRow.id == run.id, RunRow.status == expected_status.value)
            .values(
                status=run.status.value,
                status_reason=run.status_reason,
                exit_code=run.exit_code,
                external_ref=run.external_ref,
                cancel_requested=run.cancel_requested,
                updated_at=run.updated_at,
                started_at=run.started_at,
                finished_at=run.finished_at,
                workflow_cleaned_at=run.workflow_cleaned_at,
            )
        )
        if getattr(result, "rowcount", 0) == 1:
            return
        if self._s.get(RunRow, run.id) is None:
            raise NotFound("run", run.id)
        raise Conflict(f"run {run.id} is no longer {expected_status.value}")


def _definition(row: PipelineDefinitionRow) -> PipelineDefinition:
    return PipelineDefinition(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        version=row.version,
        steps=tuple(
            StepSpec(name=d["name"], job=d["job"], depends_on=tuple(d["depends_on"]))
            for d in row.steps
        ),
        parameter_schema=dict(row.parameter_schema),
        created_at=row.created_at,
    )


def _pipeline_run(row: PipelineRunRow) -> PipelineRun:
    return PipelineRun(
        id=row.id,
        project_id=row.project_id,
        pipeline_definition_id=row.pipeline_definition_id,
        status=RunStatus(row.status),
        status_reason=row.status_reason,
        external_ref=row.external_ref,
        cancel_requested=row.cancel_requested,
        commit_sha=row.commit_sha,
        idempotency_key=row.idempotency_key,
        traceparent=row.traceparent,
        timeout_seconds=row.timeout_seconds,
        parameters=dict(row.parameters),
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        workflow_cleaned_at=row.workflow_cleaned_at,
        models_discovered_at=row.models_discovered_at,
        model_discovery_checked_at=row.model_discovery_checked_at,
    )


def _step_run(row: StepRunRow) -> StepRun:
    return StepRun(
        id=row.id,
        pipeline_run_id=row.pipeline_run_id,
        step_name=row.step_name,
        status=StepStatus(row.status),
        status_reason=row.status_reason,
        exit_code=row.exit_code,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


class SqlPipelines:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, definition: PipelineDefinition) -> None:
        self._s.add(
            PipelineDefinitionRow(
                id=definition.id,
                project_id=definition.project_id,
                name=definition.name,
                version=definition.version,
                steps=[
                    {"name": st.name, "job": st.job, "depends_on": list(st.depends_on)}
                    for st in definition.steps
                ],
                parameter_schema=dict(definition.parameter_schema),
                created_at=definition.created_at,
            )
        )
        _flush_unique(self._s, "pipeline", f"{definition.name}@{definition.version}")

    def get(self, definition_id: UUID) -> PipelineDefinition | None:
        row = self._s.get(PipelineDefinitionRow, definition_id)
        return _definition(row) if row else None

    def get_version(
        self, project_id: UUID, name: str, version: int | None
    ) -> PipelineDefinition | None:
        stmt = select(PipelineDefinitionRow).where(
            PipelineDefinitionRow.project_id == project_id, PipelineDefinitionRow.name == name
        )
        if version is not None:
            stmt = stmt.where(PipelineDefinitionRow.version == version)
        row = self._s.scalars(stmt.order_by(PipelineDefinitionRow.version.desc())).first()
        return _definition(row) if row else None

    def list_latest(self, project_id: UUID) -> Sequence[PipelineDefinition]:
        rows = self._s.scalars(
            select(PipelineDefinitionRow)
            .where(PipelineDefinitionRow.project_id == project_id)
            .order_by(PipelineDefinitionRow.name, PipelineDefinitionRow.version.desc())
        )
        latest: dict[str, PipelineDefinitionRow] = {}
        for row in rows:
            latest.setdefault(row.name, row)
        return [_definition(r) for r in latest.values()]


class SqlPipelineRuns:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, run: PipelineRun) -> None:
        self._s.add(
            PipelineRunRow(
                id=run.id,
                project_id=run.project_id,
                pipeline_definition_id=run.pipeline_definition_id,
                status=run.status.value,
                status_reason=run.status_reason,
                external_ref=run.external_ref,
                cancel_requested=run.cancel_requested,
                commit_sha=run.commit_sha,
                idempotency_key=run.idempotency_key,
                traceparent=run.traceparent,
                timeout_seconds=run.timeout_seconds,
                parameters=dict(run.parameters),
                created_at=run.created_at,
                updated_at=run.updated_at,
                started_at=run.started_at,
                finished_at=run.finished_at,
                workflow_cleaned_at=run.workflow_cleaned_at,
                models_discovered_at=run.models_discovered_at,
                model_discovery_checked_at=run.model_discovery_checked_at,
            )
        )
        _flush_unique(self._s, "pipeline run", run.idempotency_key)

    def get(self, run_id: UUID) -> PipelineRun | None:
        row = self._s.get(PipelineRunRow, run_id)
        return _pipeline_run(row) if row else None

    def get_by_idempotency_key(self, project_id: UUID, key: str) -> PipelineRun | None:
        row = self._s.scalars(
            select(PipelineRunRow).where(
                PipelineRunRow.project_id == project_id, PipelineRunRow.idempotency_key == key
            )
        ).first()
        return _pipeline_run(row) if row else None

    def list(
        self,
        project_id: UUID,
        *,
        definition_ids: Sequence[UUID] | None,
        limit: int,
        offset: int,
        statuses: Collection[RunStatus] | None = None,
        finished_since: datetime | None = None,
    ) -> Sequence[PipelineRun]:
        stmt = select(PipelineRunRow).where(PipelineRunRow.project_id == project_id)
        if finished_since is not None:
            stmt = stmt.where(
                func.coalesce(PipelineRunRow.finished_at, PipelineRunRow.updated_at)
                >= finished_since
            )
        if statuses:
            stmt = stmt.where(PipelineRunRow.status.in_([s.value for s in statuses]))
        if definition_ids is not None:
            stmt = stmt.where(PipelineRunRow.pipeline_definition_id.in_(list(definition_ids)))
        stmt = stmt.order_by(PipelineRunRow.created_at.desc(), PipelineRunRow.id.desc())
        return [_pipeline_run(r) for r in self._s.scalars(stmt.limit(limit).offset(offset))]

    def count(self, project_id: UUID) -> int:
        return int(
            self._s.scalar(
                select(func.count())
                .select_from(PipelineRunRow)
                .where(PipelineRunRow.project_id == project_id)
            )
            or 0
        )

    def list_discovery_candidates(self, before: datetime, limit: int) -> Sequence[PipelineRun]:
        rows = self._s.scalars(
            select(PipelineRunRow)
            .where(
                PipelineRunRow.status == RunStatus.SUCCEEDED.value,
                PipelineRunRow.models_discovered_at.is_(None),
                PipelineRunRow.finished_at <= before,
                (
                    PipelineRunRow.model_discovery_checked_at.is_(None)
                    | (PipelineRunRow.model_discovery_checked_at <= before)
                ),
            )
            .order_by(
                func.coalesce(
                    PipelineRunRow.model_discovery_checked_at, PipelineRunRow.finished_at
                ),
                PipelineRunRow.id,
            )
            .limit(limit)
        )
        return [_pipeline_run(r) for r in rows]

    def mark_model_discovery(self, run_id: UUID, checked_at: datetime, completed: bool) -> bool:
        result = self._s.execute(
            update(PipelineRunRow)
            .where(
                PipelineRunRow.id == run_id,
                PipelineRunRow.status == RunStatus.SUCCEEDED.value,
                PipelineRunRow.models_discovered_at.is_(None),
            )
            .values(
                model_discovery_checked_at=checked_at,
                models_discovered_at=checked_at if completed else None,
            )
        )
        return getattr(result, "rowcount", 0) == 1

    def list_active(self) -> Sequence[PipelineRun]:
        terminal = [s.value for s in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)]
        rows = self._s.scalars(
            select(PipelineRunRow)
            .where(PipelineRunRow.status.not_in(terminal))
            .order_by(PipelineRunRow.created_at, PipelineRunRow.id)
        )
        return [_pipeline_run(r) for r in rows]

    def list_cleanup_candidates(self, before: datetime, limit: int) -> Sequence[PipelineRun]:
        rows = self._s.scalars(
            select(PipelineRunRow)
            .where(
                PipelineRunRow.status.in_(
                    [s.value for s in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)]
                ),
                PipelineRunRow.finished_at < before,
                PipelineRunRow.external_ref.is_not(None),
                PipelineRunRow.workflow_cleaned_at.is_(None),
            )
            .order_by(PipelineRunRow.finished_at, PipelineRunRow.id)
            .limit(limit)
        )
        return [_pipeline_run(r) for r in rows]

    def update(self, run: PipelineRun, *, expected_status: RunStatus) -> None:
        result = self._s.execute(
            update(PipelineRunRow)
            .where(PipelineRunRow.id == run.id, PipelineRunRow.status == expected_status.value)
            .values(
                status=run.status.value,
                status_reason=run.status_reason,
                external_ref=run.external_ref,
                cancel_requested=run.cancel_requested,
                updated_at=run.updated_at,
                started_at=run.started_at,
                finished_at=run.finished_at,
                workflow_cleaned_at=run.workflow_cleaned_at,
            )
        )
        if getattr(result, "rowcount", 0) == 1:
            return
        if self._s.get(PipelineRunRow, run.id) is None:
            raise NotFound("pipeline run", run.id)
        raise Conflict(f"pipeline run {run.id} is no longer {expected_status.value}")


class SqlStepRuns:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add_many(self, steps: Sequence[StepRun]) -> None:
        for step in steps:
            self._s.add(
                StepRunRow(
                    id=step.id,
                    pipeline_run_id=step.pipeline_run_id,
                    step_name=step.step_name,
                    status=step.status.value,
                    status_reason=step.status_reason,
                    exit_code=step.exit_code,
                    created_at=step.created_at,
                    updated_at=step.updated_at,
                    started_at=step.started_at,
                    finished_at=step.finished_at,
                )
            )
        self._s.flush()

    def list(self, pipeline_run_id: UUID) -> Sequence[StepRun]:
        rows = self._s.scalars(
            select(StepRunRow)
            .where(StepRunRow.pipeline_run_id == pipeline_run_id)
            .order_by(StepRunRow.created_at, StepRunRow.step_name)
        )
        return [_step_run(r) for r in rows]

    def update(self, step: StepRun, *, expected_status: StepStatus) -> None:
        result = self._s.execute(
            update(StepRunRow)
            .where(StepRunRow.id == step.id, StepRunRow.status == expected_status.value)
            .values(
                status=step.status.value,
                status_reason=step.status_reason,
                exit_code=step.exit_code,
                updated_at=step.updated_at,
                started_at=step.started_at,
                finished_at=step.finished_at,
            )
        )
        if getattr(result, "rowcount", 0) == 1:
            return
        if self._s.get(StepRunRow, step.id) is None:
            raise NotFound("step run", step.id)
        raise Conflict(f"step run {step.id} is no longer {expected_status.value}")


def _threshold_json(t: Threshold) -> dict[str, float | None]:
    return {"min": t.min, "max": t.max}


def _model(row: ModelRow) -> Model:
    return Model(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        thresholds={k: Threshold(min=v["min"], max=v["max"]) for k, v in row.thresholds.items()},
        alias_drift=row.alias_drift,
        kind=ModelKind(row.kind),
        serving=(
            LlmServing(
                gpus=row.llm_gpus,
                context_length=row.llm_context_length,
                min_scale=row.llm_min_scale,
                max_scale=row.llm_max_scale,
            )
            if row.llm_gpus is not None
            else None
        ),
        function=FunctionServing.from_json(row.function_settings)
        if row.function_settings
        else None,
        secret_refs=SecretRefs.from_json(row.secret_refs),
        created_at=row.created_at,
    )


def _version(row: ModelVersionRow) -> ModelVersion:
    return ModelVersion(
        id=row.id,
        model_id=row.model_id,
        version=row.version,
        status=ModelStatus(row.status),
        external_ref=row.external_ref,
        source_pipeline_run_id=row.source_pipeline_run_id,
        source_uri=row.source_uri,
        metrics=dict(row.metrics or {}),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _evaluation(row: EvaluationRow) -> Evaluation:
    return Evaluation(
        id=row.id,
        model_version_id=row.model_version_id,
        baseline_version_id=row.baseline_version_id,
        metrics=dict(row.metrics),
        checks=tuple(
            Check(
                metric=c["metric"],
                value=c["value"],
                threshold=Threshold(min=c["min"], max=c["max"]),
                passed=c["passed"],
            )
            for c in row.checks
        ),
        status=EvaluationStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _checks_json(checks: Sequence[Check]) -> list[dict[str, Any]]:
    return [
        {
            "metric": c.metric,
            "value": c.value,
            "min": c.threshold.min,
            "max": c.threshold.max,
            "passed": c.passed,
        }
        for c in checks
    ]


class SqlModels:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, model: Model) -> None:
        self._s.add(
            ModelRow(
                id=model.id,
                project_id=model.project_id,
                name=model.name,
                thresholds={k: _threshold_json(v) for k, v in model.thresholds.items()},
                alias_drift=model.alias_drift,
                kind=model.kind.value,
                llm_gpus=model.serving.gpus if model.serving else None,
                llm_context_length=model.serving.context_length if model.serving else None,
                llm_min_scale=model.serving.min_scale if model.serving else 1,
                llm_max_scale=model.serving.max_scale if model.serving else 1,
                function_settings=model.function.to_json() if model.function else None,
                secret_refs=model.secret_refs.to_json(),
                created_at=model.created_at,
            )
        )
        _flush_unique(self._s, "model", model.name)

    def get(self, model_id: UUID) -> Model | None:
        row = self._s.get(ModelRow, model_id)
        return _model(row) if row else None

    def get_by_name(self, project_id: UUID, name: str) -> Model | None:
        row = self._s.scalars(
            select(ModelRow).where(ModelRow.project_id == project_id, ModelRow.name == name)
        ).first()
        return _model(row) if row else None

    def list(self, project_id: UUID) -> Sequence[Model]:
        rows = self._s.scalars(
            select(ModelRow)
            .where(ModelRow.project_id == project_id)
            .order_by(ModelRow.created_at, ModelRow.id)
        )
        return [_model(r) for r in rows]

    def list_all(self) -> Sequence[Model]:
        return [
            _model(r)
            for r in self._s.scalars(select(ModelRow).order_by(ModelRow.created_at, ModelRow.id))
        ]

    def update_thresholds(self, model: Model) -> None:
        result = self._s.execute(
            update(ModelRow)
            .where(ModelRow.id == model.id)
            .values(
                thresholds={k: _threshold_json(v) for k, v in model.thresholds.items()},
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            raise NotFound("model", model.id)

    def update_alias_drift(self, model: Model) -> None:
        result = self._s.execute(
            update(ModelRow).where(ModelRow.id == model.id).values(alias_drift=model.alias_drift)
        )
        if getattr(result, "rowcount", 0) != 1:
            raise NotFound("model", model.id)


class SqlModelVersions:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, version: ModelVersion) -> None:
        self._s.add(
            ModelVersionRow(
                id=version.id,
                model_id=version.model_id,
                version=version.version,
                status=version.status.value,
                external_ref=version.external_ref,
                source_pipeline_run_id=version.source_pipeline_run_id,
                source_uri=version.source_uri,
                metrics=dict(version.metrics),
                created_at=version.created_at,
                updated_at=version.updated_at,
            )
        )
        _flush_unique(self._s, "model version", version.external_ref or version.version)

    def get(self, version_id: UUID) -> ModelVersion | None:
        row = self._s.get(ModelVersionRow, version_id)
        return _version(row) if row else None

    def lock(self, version_id: UUID) -> ModelVersion | None:
        row = self._s.scalars(
            select(ModelVersionRow)
            .where(ModelVersionRow.id == version_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        return _version(row) if row else None

    def get_by_ref(self, model_id: UUID, external_ref: str) -> ModelVersion | None:
        row = self._s.scalars(
            select(ModelVersionRow).where(
                ModelVersionRow.model_id == model_id, ModelVersionRow.external_ref == external_ref
            )
        ).first()
        return _version(row) if row else None

    def list(self, model_id: UUID) -> Sequence[ModelVersion]:
        rows = self._s.scalars(
            select(ModelVersionRow)
            .where(ModelVersionRow.model_id == model_id)
            .order_by(ModelVersionRow.version)
        )
        return [_version(r) for r in rows]

    def next_version(self, model_id: UUID) -> int:
        current = self._s.scalar(
            select(func.max(ModelVersionRow.version)).where(ModelVersionRow.model_id == model_id)
        )
        return (current or 0) + 1

    def get_champion(self, model_id: UUID) -> ModelVersion | None:
        row = self._s.scalars(
            select(ModelVersionRow).where(
                ModelVersionRow.model_id == model_id,
                ModelVersionRow.status == ModelStatus.CHAMPION.value,
            )
        ).first()
        return _version(row) if row else None

    def update(self, version: ModelVersion, *, expected_status: ModelStatus) -> None:
        try:
            result = self._s.execute(
                update(ModelVersionRow)
                .where(
                    ModelVersionRow.id == version.id,
                    ModelVersionRow.status == expected_status.value,
                )
                .values(status=version.status.value, updated_at=version.updated_at)
            )
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION:
                raise Conflict(f"model {version.model_id} already has a champion") from exc
            raise
        if getattr(result, "rowcount", 0) == 1:
            return
        if self._s.get(ModelVersionRow, version.id) is None:
            raise NotFound("model version", version.id)
        raise Conflict(f"model version {version.id} is no longer {expected_status.value}")


class SqlEvaluations:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, evaluation: Evaluation) -> None:
        self._s.add(
            EvaluationRow(
                id=evaluation.id,
                model_version_id=evaluation.model_version_id,
                baseline_version_id=evaluation.baseline_version_id,
                metrics=dict(evaluation.metrics),
                checks=_checks_json(evaluation.checks),
                status=evaluation.status.value,
                created_at=evaluation.created_at,
                updated_at=evaluation.updated_at,
            )
        )
        self._s.flush()

    def get(self, evaluation_id: UUID) -> Evaluation | None:
        row = self._s.get(EvaluationRow, evaluation_id)
        return _evaluation(row) if row else None

    def list_for_version(self, version_id: UUID) -> Sequence[Evaluation]:
        rows = self._s.scalars(
            select(EvaluationRow)
            .where(EvaluationRow.model_version_id == version_id)
            .order_by(EvaluationRow.created_at, EvaluationRow.id)
        )
        return [_evaluation(r) for r in rows]

    def update(self, evaluation: Evaluation, *, expected_status: EvaluationStatus) -> None:
        result = self._s.execute(
            update(EvaluationRow)
            .where(EvaluationRow.id == evaluation.id, EvaluationRow.status == expected_status.value)
            .values(
                status=evaluation.status.value,
                metrics=dict(evaluation.metrics),
                checks=_checks_json(evaluation.checks),
                updated_at=evaluation.updated_at,
            )
        )
        if getattr(result, "rowcount", 0) == 1:
            return
        if self._s.get(EvaluationRow, evaluation.id) is None:
            raise NotFound("evaluation", evaluation.id)
        raise Conflict(f"evaluation {evaluation.id} is no longer {expected_status.value}")


class SqlPromotions:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, promotion: Promotion) -> None:
        self._s.add(
            PromotionRow(
                id=promotion.id,
                model_version_id=promotion.model_version_id,
                previous_champion_id=promotion.previous_champion_id,
                status=promotion.status.value,
                created_at=promotion.created_at,
                updated_at=promotion.updated_at,
            )
        )
        self._s.flush()

    def list_for_versions(self, version_ids: Sequence[UUID]) -> Sequence[Promotion]:
        rows = self._s.scalars(
            select(PromotionRow)
            .where(PromotionRow.model_version_id.in_(list(version_ids)))
            .order_by(PromotionRow.created_at, PromotionRow.id)
        )
        return [
            Promotion(
                id=r.id,
                model_version_id=r.model_version_id,
                previous_champion_id=r.previous_champion_id,
                status=PromotionStatus(r.status),
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r in rows
        ]


def _deployment(row: DeploymentRow) -> Deployment:
    return Deployment(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        status=DeploymentStatus(row.status),
        status_reason=row.status_reason,
        desired_revision=row.desired_revision,
        traceparent=row.traceparent,
        active_revision=row.active_revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _revision(row: DeploymentRevisionRow) -> DeploymentRevision:
    return DeploymentRevision(
        id=row.id,
        deployment_id=row.deployment_id,
        revision=row.revision,
        model_version_id=row.model_version_id,
        model_uri=row.model_uri,
        runtime=ServingRuntime(row.runtime),
        gpus=row.gpus,
        context_length=row.context_length,
        min_scale=row.min_scale,
        max_scale=row.max_scale,
        function=FunctionServing.from_json(row.function_settings)
        if row.function_settings
        else None,
        secret_refs=SecretRefs.from_json(row.secret_refs),
        created_at=row.created_at,
    )


def _endpoint(row: EndpointRow) -> Endpoint:
    return Endpoint(
        id=row.id,
        project_id=row.project_id,
        deployment_id=row.deployment_id,
        name=row.name,
        status=EndpointStatus(row.status),
        url=row.url,
        kind=EndpointKind(row.kind),
        protocol=EndpointProtocol(row.protocol),
        exposure=Exposure(row.exposure),
        limits=EndpointLimits(
            units_per_minute=row.limit_units_per_minute,
            max_body_kb=row.limit_max_body_kb,
            timeout_seconds=row.limit_timeout_seconds,
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _endpoint_values(endpoint: Endpoint) -> dict[str, object]:
    return {
        "status": endpoint.status.value,
        "url": endpoint.url,
        "kind": endpoint.kind.value,
        "protocol": endpoint.protocol.value,
        "exposure": endpoint.exposure.value,
        "limit_units_per_minute": endpoint.limits.units_per_minute,
        "limit_max_body_kb": endpoint.limits.max_body_kb,
        "limit_timeout_seconds": endpoint.limits.timeout_seconds,
        "updated_at": endpoint.updated_at,
    }


class SqlDeployments:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, deployment: Deployment) -> None:
        self._s.add(
            DeploymentRow(
                id=deployment.id,
                project_id=deployment.project_id,
                name=deployment.name,
                status=deployment.status.value,
                status_reason=deployment.status_reason,
                desired_revision=deployment.desired_revision,
                traceparent=deployment.traceparent,
                active_revision=deployment.active_revision,
                created_at=deployment.created_at,
                updated_at=deployment.updated_at,
            )
        )
        _flush_unique(self._s, "deployment", deployment.name)

    def get(self, deployment_id: UUID) -> Deployment | None:
        row = self._s.get(DeploymentRow, deployment_id)
        return _deployment(row) if row else None

    def lock(self, deployment_id: UUID) -> Deployment | None:
        row = self._s.scalars(
            select(DeploymentRow)
            .where(DeploymentRow.id == deployment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).first()
        return _deployment(row) if row else None

    def get_by_name(self, project_id: UUID, name: str) -> Deployment | None:
        row = self._s.scalars(
            select(DeploymentRow).where(
                DeploymentRow.project_id == project_id, DeploymentRow.name == name
            )
        ).first()
        return _deployment(row) if row else None

    def list(self, project_id: UUID) -> Sequence[Deployment]:
        rows = self._s.scalars(
            select(DeploymentRow)
            .where(DeploymentRow.project_id == project_id)
            .order_by(DeploymentRow.created_at, DeploymentRow.id)
        )
        return [_deployment(r) for r in rows]

    def list_reconcilable(self) -> Sequence[Deployment]:
        rows = self._s.scalars(
            select(DeploymentRow)
            .where(
                (DeploymentRow.status == DeploymentStatus.DELETING.value)
                | (
                    (DeploymentRow.desired_revision.is_not(None))
                    & (DeploymentRow.status != DeploymentStatus.DELETED.value)
                )
            )
            .order_by(DeploymentRow.created_at, DeploymentRow.id)
        )
        return [_deployment(r) for r in rows]

    def update(self, deployment: Deployment, *, expected_status: DeploymentStatus) -> None:
        result = self._s.execute(
            update(DeploymentRow)
            .where(DeploymentRow.id == deployment.id, DeploymentRow.status == expected_status.value)
            .values(
                status=deployment.status.value,
                status_reason=deployment.status_reason,
                desired_revision=deployment.desired_revision,
                traceparent=deployment.traceparent,
                active_revision=deployment.active_revision,
                updated_at=deployment.updated_at,
            )
        )
        if getattr(result, "rowcount", 0) == 1:
            return
        if self._s.get(DeploymentRow, deployment.id) is None:
            raise NotFound("deployment", deployment.id)
        raise Conflict(f"deployment {deployment.id} is no longer {expected_status.value}")


class SqlRevisions:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, revision: DeploymentRevision) -> None:
        self._s.add(
            DeploymentRevisionRow(
                id=revision.id,
                deployment_id=revision.deployment_id,
                revision=revision.revision,
                model_version_id=revision.model_version_id,
                model_uri=revision.model_uri,
                runtime=revision.runtime.value,
                gpus=revision.gpus,
                context_length=revision.context_length,
                min_scale=revision.min_scale,
                max_scale=revision.max_scale,
                function_settings=revision.function.to_json() if revision.function else None,
                secret_refs=revision.secret_refs.to_json(),
                created_at=revision.created_at,
            )
        )
        _flush_unique(self._s, "revision", revision.revision)

    def get(self, deployment_id: UUID, revision: int) -> DeploymentRevision | None:
        row = self._s.scalars(
            select(DeploymentRevisionRow).where(
                DeploymentRevisionRow.deployment_id == deployment_id,
                DeploymentRevisionRow.revision == revision,
            )
        ).first()
        return _revision(row) if row else None

    def list(self, deployment_id: UUID) -> Sequence[DeploymentRevision]:
        rows = self._s.scalars(
            select(DeploymentRevisionRow)
            .where(DeploymentRevisionRow.deployment_id == deployment_id)
            .order_by(DeploymentRevisionRow.revision)
        )
        return [_revision(r) for r in rows]

    def next_revision(self, deployment_id: UUID) -> int:
        current = self._s.scalar(
            select(func.max(DeploymentRevisionRow.revision)).where(
                DeploymentRevisionRow.deployment_id == deployment_id
            )
        )
        return (current or 0) + 1


class SqlEndpoints:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, endpoint: Endpoint) -> None:
        self._s.add(
            EndpointRow(
                id=endpoint.id,
                project_id=endpoint.project_id,
                deployment_id=endpoint.deployment_id,
                name=endpoint.name,
                created_at=endpoint.created_at,
                **_endpoint_values(endpoint),
            )
        )
        _flush_unique(self._s, "endpoint", endpoint.name)

    def get_by_deployment(self, deployment_id: UUID) -> Endpoint | None:
        row = self._s.scalars(
            select(EndpointRow).where(EndpointRow.deployment_id == deployment_id)
        ).first()
        return _endpoint(row) if row else None

    def get_by_name(self, project_id: UUID, name: str) -> Endpoint | None:
        row = self._s.scalars(
            select(EndpointRow).where(
                EndpointRow.project_id == project_id, EndpointRow.name == name
            )
        ).first()
        return _endpoint(row) if row else None

    def update_lifecycle(self, endpoint: Endpoint, *, expected_status: EndpointStatus) -> None:
        result = self._s.execute(
            update(EndpointRow)
            .where(EndpointRow.id == endpoint.id, EndpointRow.status == expected_status.value)
            .values(
                **{
                    k: v
                    for k, v in _endpoint_values(endpoint).items()
                    if k
                    not in {
                        "exposure",
                        "limit_units_per_minute",
                        "limit_max_body_kb",
                        "limit_timeout_seconds",
                    }
                }
            )
        )
        if getattr(result, "rowcount", 0) == 1:
            return
        if self._s.get(EndpointRow, endpoint.id) is None:
            raise NotFound("endpoint", endpoint.id)
        raise Conflict(f"endpoint {endpoint.id} is no longer {expected_status.value}")

    def initialize_limits(self, endpoint: Endpoint, *, expected_updated_at: datetime) -> None:
        defaults = EndpointLimits()
        self._s.execute(
            update(EndpointRow)
            .where(
                EndpointRow.id == endpoint.id,
                EndpointRow.updated_at == expected_updated_at,
                EndpointRow.limit_units_per_minute == defaults.units_per_minute,
                EndpointRow.limit_max_body_kb == defaults.max_body_kb,
                EndpointRow.limit_timeout_seconds == defaults.timeout_seconds,
            )
            .values(
                limit_units_per_minute=endpoint.limits.units_per_minute,
                limit_max_body_kb=endpoint.limits.max_body_kb,
                limit_timeout_seconds=endpoint.limits.timeout_seconds,
            )
        )

    def update_access(self, endpoint: Endpoint) -> None:
        values = _endpoint_values(endpoint)
        result = self._s.execute(
            update(EndpointRow)
            .where(EndpointRow.id == endpoint.id)
            .values(
                **{
                    k: values[k]
                    for k in (
                        "exposure",
                        "limit_units_per_minute",
                        "limit_max_body_kb",
                        "limit_timeout_seconds",
                        "updated_at",
                    )
                }
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            raise NotFound("endpoint", endpoint.id)


def _rollout(row: RolloutRow) -> Rollout:
    return Rollout(
        id=row.id,
        deployment_id=row.deployment_id,
        from_revision=row.from_revision,
        to_revision=row.to_revision,
        model_version_id=row.model_version_id,
        status=RolloutStatus(row.status),
        status_reason=row.status_reason,
        steps=tuple(row.steps),
        current_step=row.current_step,
        gate=RolloutGate(**row.gate),
        step_started_at=row.step_started_at,
        abort_requested=row.abort_requested,
        traceparent=row.traceparent,
        created_at=row.created_at,
        updated_at=row.updated_at,
        finished_at=row.finished_at,
    )


_GATE_FIELDS = (
    "max_error_rate",
    "max_p95_latency_ms",
    "min_requests",
    "step_seconds",
    "ready_timeout_seconds",
)


class SqlRollouts:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, rollout: Rollout) -> None:
        self._s.add(
            RolloutRow(
                id=rollout.id,
                deployment_id=rollout.deployment_id,
                from_revision=rollout.from_revision,
                to_revision=rollout.to_revision,
                model_version_id=rollout.model_version_id,
                status=rollout.status.value,
                status_reason=rollout.status_reason,
                steps=list(rollout.steps),
                current_step=rollout.current_step,
                gate={f: getattr(rollout.gate, f) for f in _GATE_FIELDS},
                step_started_at=rollout.step_started_at,
                abort_requested=rollout.abort_requested,
                traceparent=rollout.traceparent,
                created_at=rollout.created_at,
                updated_at=rollout.updated_at,
                finished_at=rollout.finished_at,
            )
        )
        _flush_unique(self._s, "rollout", rollout.deployment_id)

    def get(self, rollout_id: UUID) -> Rollout | None:
        row = self._s.get(RolloutRow, rollout_id)
        return _rollout(row) if row else None

    def get_active(self, deployment_id: UUID) -> Rollout | None:
        row = self._s.scalars(
            select(RolloutRow).where(
                RolloutRow.deployment_id == deployment_id,
                RolloutRow.status.in_(
                    [RolloutStatus.PENDING.value, RolloutStatus.PROGRESSING.value]
                ),
            )
        ).first()
        return _rollout(row) if row else None

    def get_active_by_version(self, version_id: UUID) -> Rollout | None:
        row = self._s.scalars(
            select(RolloutRow).where(
                RolloutRow.model_version_id == version_id,
                RolloutRow.status.in_(
                    [RolloutStatus.PENDING.value, RolloutStatus.PROGRESSING.value]
                ),
            )
        ).first()
        return _rollout(row) if row else None

    def list(self, deployment_id: UUID) -> Sequence[Rollout]:
        rows = self._s.scalars(
            select(RolloutRow)
            .where(RolloutRow.deployment_id == deployment_id)
            .order_by(RolloutRow.created_at.desc(), RolloutRow.id.desc())
        )
        return [_rollout(r) for r in rows]

    def list_active(self) -> Sequence[Rollout]:
        rows = self._s.scalars(
            select(RolloutRow)
            .where(
                RolloutRow.status.in_(
                    [RolloutStatus.PENDING.value, RolloutStatus.PROGRESSING.value]
                )
            )
            .order_by(RolloutRow.created_at, RolloutRow.id)
        )
        return [_rollout(r) for r in rows]

    def update(self, rollout: Rollout, *, expected_status: RolloutStatus) -> None:
        result = self._s.execute(
            update(RolloutRow)
            .where(RolloutRow.id == rollout.id, RolloutRow.status == expected_status.value)
            .values(
                status=rollout.status.value,
                status_reason=rollout.status_reason,
                current_step=rollout.current_step,
                step_started_at=rollout.step_started_at,
                abort_requested=rollout.abort_requested,
                updated_at=rollout.updated_at,
                finished_at=rollout.finished_at,
            )
        )
        if getattr(result, "rowcount", 0) == 1:
            return
        if self._s.get(RolloutRow, rollout.id) is None:
            raise NotFound("rollout", rollout.id)
        raise Conflict(f"rollout {rollout.id} is no longer {expected_status.value}")


class SqlAudit:
    def __init__(self, session: Session) -> None:
        self._s = session

    def latest(
        self, *, project_id: UUID, entity_type: str, entity_id: UUID, actions: Sequence[str]
    ) -> AuditEvent | None:
        row = self._s.scalars(
            select(AuditEventRow)
            .where(
                AuditEventRow.project_id == project_id,
                AuditEventRow.entity_type == entity_type,
                AuditEventRow.entity_id == entity_id,
                AuditEventRow.action.in_(actions),
            )
            .order_by(AuditEventRow.occurred_at.desc(), AuditEventRow.id.desc())
            .limit(1)
        ).first()
        return _audit(row) if row else None

    def record(self, event: AuditEvent) -> None:
        self._s.add(
            AuditEventRow(
                id=event.id,
                occurred_at=event.occurred_at,
                actor=event.actor,
                action=event.action,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                project_id=event.project_id,
                payload=dict(event.payload),
                trace_id=event.trace_id,
            )
        )

    def list(
        self, *, project_id: UUID | None = None, entity_id: UUID | None = None
    ) -> Sequence[AuditEvent]:
        stmt = select(AuditEventRow).order_by(AuditEventRow.occurred_at, AuditEventRow.id)
        if project_id is not None:
            stmt = stmt.where(AuditEventRow.project_id == project_id)
        if entity_id is not None:
            stmt = stmt.where(AuditEventRow.entity_id == entity_id)
        return [_audit(r) for r in self._s.scalars(stmt)]


def _membership(row: MembershipRow) -> Membership:
    return Membership(
        id=row.id,
        project_id=row.project_id,
        subject=row.subject,
        role=ProjectRole(row.role),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlMemberships:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, membership: Membership) -> None:
        self._s.add(
            MembershipRow(
                id=membership.id,
                project_id=membership.project_id,
                subject=membership.subject,
                role=membership.role.value,
                created_at=membership.created_at,
                updated_at=membership.updated_at,
            )
        )
        _flush_unique(self._s, "member", membership.subject)

    def get(self, project_id: UUID, subject: str) -> Membership | None:
        row = self._s.scalars(
            select(MembershipRow).where(
                MembershipRow.project_id == project_id, MembershipRow.subject == subject
            )
        ).first()
        return _membership(row) if row else None

    def list(self, project_id: UUID) -> Sequence[Membership]:
        rows = self._s.scalars(
            select(MembershipRow)
            .where(MembershipRow.project_id == project_id)
            .order_by(MembershipRow.subject)
        )
        return [_membership(r) for r in rows]

    def list_for_subjects(self, subjects: Collection[str]) -> Sequence[Membership]:
        if not subjects:
            return []
        rows = self._s.scalars(
            select(MembershipRow).where(MembershipRow.subject.in_(list(subjects)))
        )
        return [_membership(r) for r in rows]

    def update(self, membership: Membership) -> None:
        result = self._s.execute(
            update(MembershipRow)
            .where(MembershipRow.id == membership.id)
            .values(role=membership.role.value, updated_at=membership.updated_at)
        )
        if getattr(result, "rowcount", 0) != 1:
            raise NotFound("member", membership.subject)

    def remove(self, project_id: UUID, subject: str) -> None:
        result = self._s.execute(
            delete(MembershipRow).where(
                MembershipRow.project_id == project_id, MembershipRow.subject == subject
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            raise NotFound("member", subject)


def _api_key(row: ApiKeyRow) -> ApiKey:
    return ApiKey(
        id=row.id,
        key_id=row.key_id,
        project_id=row.project_id,
        name=row.name,
        endpoints=tuple(row.endpoints),
        units_per_minute=row.units_per_minute,
        secret_hash=row.secret_hash,
        created_by=row.created_by,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        last_used_at=row.last_used_at,
    )


class SqlApiKeys:
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, key: ApiKey) -> None:
        self._s.add(
            ApiKeyRow(
                id=key.id,
                key_id=key.key_id,
                project_id=key.project_id,
                name=key.name,
                endpoints=list(key.endpoints),
                units_per_minute=key.units_per_minute,
                secret_hash=key.secret_hash,
                created_by=key.created_by,
                created_at=key.created_at,
                expires_at=key.expires_at,
                revoked_at=key.revoked_at,
                last_used_at=key.last_used_at,
            )
        )
        _flush_unique(self._s, "api key", key.name)

    def get(self, key_id: str) -> ApiKey | None:
        row = self._s.scalars(select(ApiKeyRow).where(ApiKeyRow.key_id == key_id)).first()
        return _api_key(row) if row else None

    def list(self, project_id: UUID) -> Sequence[ApiKey]:
        rows = self._s.scalars(
            select(ApiKeyRow)
            .where(ApiKeyRow.project_id == project_id)
            .order_by(ApiKeyRow.created_at.desc())
        )
        return [_api_key(r) for r in rows]

    def update(self, key: ApiKey) -> None:
        result = self._s.execute(
            update(ApiKeyRow)
            .where(ApiKeyRow.key_id == key.key_id)
            .values(
                endpoints=list(key.endpoints),
                units_per_minute=key.units_per_minute,
                expires_at=key.expires_at,
                revoked_at=key.revoked_at,
                last_used_at=key.last_used_at,
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            raise NotFound("api key", key.key_id)


    def touch(self, key_id: str, at: datetime) -> None:
        self._s.execute(
            update(ApiKeyRow).where(ApiKeyRow.key_id == key_id).values(last_used_at=at)
        )


class SqlNotificationReads:
    def __init__(self, session: Session) -> None:
        self._s = session

    def find(self, username: str, ids: Sequence[str]) -> set[str]:
        if not ids:
            return set()
        return set(
            self._s.scalars(
                select(NotificationReadRow.notification_id).where(
                    NotificationReadRow.username == username,
                    NotificationReadRow.notification_id.in_(ids),
                )
            )
        )

    def mark(self, username: str, ids: Sequence[str], at: datetime) -> None:
        if ids:
            self._s.execute(
                insert(NotificationReadRow)
                .values(
                    [
                        {"username": username, "notification_id": key, "read_at": at}
                        for key in set(ids)
                    ]
                )
                .on_conflict_do_nothing()
            )


class SqlUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory

    def __enter__(self) -> Self:
        self._session = self._factory()
        self.notification_reads = SqlNotificationReads(self._session)
        from controlplane.persistence.schedules import SqlSchedules

        self.schedules = SqlSchedules(self._session)
        self.projects = SqlProjects(self._session)
        self.jobs = SqlJobs(self._session)
        self.runs = SqlRuns(self._session)
        self.pipelines = SqlPipelines(self._session)
        self.pipeline_runs = SqlPipelineRuns(self._session)
        self.step_runs = SqlStepRuns(self._session)
        self.models = SqlModels(self._session)
        self.model_versions = SqlModelVersions(self._session)
        self.evaluations = SqlEvaluations(self._session)
        self.promotions = SqlPromotions(self._session)
        self.deployments = SqlDeployments(self._session)
        self.revisions = SqlRevisions(self._session)
        self.endpoints = SqlEndpoints(self._session)
        self.rollouts = SqlRollouts(self._session)
        self.audit = SqlAudit(self._session)
        self.memberships = SqlMemberships(self._session)
        self.api_keys = SqlApiKeys(self._session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._session.rollback()  # a no-op after commit; discards anything uncommitted
        self._session.close()

    def commit(self) -> None:
        self._session.commit()


def sql_uow_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
