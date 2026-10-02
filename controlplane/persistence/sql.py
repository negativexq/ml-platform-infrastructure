from __future__ import annotations

from collections.abc import Sequence
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from sqlalchemy import Engine, create_engine, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql import Select

from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import JobDefinition, Project, Run
from controlplane.domain.errors import AlreadyExists, Conflict, NotFound
from controlplane.domain.states import ProjectStatus, RunStatus
from controlplane.persistence.models import AuditEventRow, JobDefinitionRow, ProjectRow, RunRow

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
