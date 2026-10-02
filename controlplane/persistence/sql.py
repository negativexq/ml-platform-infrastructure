from __future__ import annotations

from collections.abc import Sequence
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from sqlalchemy import Engine, create_engine, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import Select

from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import (
    Check,
    Deployment,
    DeploymentRevision,
    Endpoint,
    Evaluation,
    JobDefinition,
    Model,
    ModelVersion,
    PipelineDefinition,
    PipelineRun,
    Project,
    Promotion,
    Run,
    StepRun,
    StepSpec,
    Threshold,
)
from controlplane.domain.errors import AlreadyExists, Conflict, NotFound
from controlplane.domain.states import (
    DeploymentStatus,
    EndpointStatus,
    EvaluationStatus,
    ModelStatus,
    ProjectStatus,
    PromotionStatus,
    RunStatus,
    StepStatus,
)
from controlplane.persistence.models import (
    AuditEventRow,
    DeploymentRevisionRow,
    DeploymentRow,
    EndpointRow,
    EvaluationRow,
    JobDefinitionRow,
    ModelRow,
    ModelVersionRow,
    PipelineDefinitionRow,
    PipelineRunRow,
    ProjectRow,
    PromotionRow,
    RunRow,
    StepRunRow,
)

_UNIQUE_VIOLATION = "23505"


def make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


def _project(row: ProjectRow) -> Project:
    return Project(
        id=row.id,
        name=row.name,
        display_name=row.display_name,
        description=row.description,
        status=ProjectStatus(row.status),
        status_reason=row.status_reason,
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
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
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
                created_at=run.created_at,
                updated_at=run.updated_at,
                started_at=run.started_at,
                finished_at=run.finished_at,
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
        self, project_id: UUID, *, job_id: UUID | None, limit: int, offset: int
    ) -> Sequence[Run]:
        stmt = select(RunRow).where(RunRow.project_id == project_id)
        if job_id is not None:
            stmt = stmt.where(RunRow.job_definition_id == job_id)
        stmt = stmt.order_by(RunRow.created_at.desc(), RunRow.id.desc()).limit(limit).offset(offset)
        return [_run(r) for r in self._s.scalars(stmt)]

    def list_active(self) -> Sequence[Run]:
        terminal = [s.value for s in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)]
        rows = self._s.scalars(
            select(RunRow)
            .where(RunRow.status.not_in(terminal))
            .order_by(RunRow.created_at, RunRow.id)
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
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
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
                created_at=run.created_at,
                updated_at=run.updated_at,
                started_at=run.started_at,
                finished_at=run.finished_at,
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
        self, project_id: UUID, *, definition_ids: Sequence[UUID] | None, limit: int, offset: int
    ) -> Sequence[PipelineRun]:
        stmt = select(PipelineRunRow).where(PipelineRunRow.project_id == project_id)
        if definition_ids is not None:
            stmt = stmt.where(PipelineRunRow.pipeline_definition_id.in_(list(definition_ids)))
        stmt = stmt.order_by(PipelineRunRow.created_at.desc(), PipelineRunRow.id.desc())
        return [_pipeline_run(r) for r in self._s.scalars(stmt.limit(limit).offset(offset))]

    def list_active(self) -> Sequence[PipelineRun]:
        terminal = [s.value for s in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED)]
        rows = self._s.scalars(
            select(PipelineRunRow)
            .where(PipelineRunRow.status.not_in(terminal))
            .order_by(PipelineRunRow.created_at, PipelineRunRow.id)
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

    def update(self, model: Model) -> None:
        result = self._s.execute(
            update(ModelRow)
            .where(ModelRow.id == model.id)
            .values(
                thresholds={k: _threshold_json(v) for k, v in model.thresholds.items()},
                alias_drift=model.alias_drift,
            )
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
                created_at=version.created_at,
                updated_at=version.updated_at,
            )
        )
        _flush_unique(self._s, "model version", version.external_ref or version.version)

    def get(self, version_id: UUID) -> ModelVersion | None:
        row = self._s.get(ModelVersionRow, version_id)
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
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


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
                active_revision=deployment.active_revision,
                created_at=deployment.created_at,
                updated_at=deployment.updated_at,
            )
        )
        _flush_unique(self._s, "deployment", deployment.name)

    def get(self, deployment_id: UUID) -> Deployment | None:
        row = self._s.get(DeploymentRow, deployment_id)
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
            .where(DeploymentRow.desired_revision.is_not(None))
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
                status=endpoint.status.value,
                url=endpoint.url,
                created_at=endpoint.created_at,
                updated_at=endpoint.updated_at,
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

    def update(self, endpoint: Endpoint, *, expected_status: EndpointStatus) -> None:
        result = self._s.execute(
            update(EndpointRow)
            .where(EndpointRow.id == endpoint.id, EndpointRow.status == expected_status.value)
            .values(status=endpoint.status.value, url=endpoint.url, updated_at=endpoint.updated_at)
        )
        if getattr(result, "rowcount", 0) == 1:
            return
        if self._s.get(EndpointRow, endpoint.id) is None:
            raise NotFound("endpoint", endpoint.id)
        raise Conflict(f"endpoint {endpoint.id} is no longer {expected_status.value}")


class SqlAudit:
    def __init__(self, session: Session) -> None:
        self._s = session

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


class SqlUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._factory = session_factory

    def __enter__(self) -> Self:
        self._session = self._factory()
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
        self.audit = SqlAudit(self._session)
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
