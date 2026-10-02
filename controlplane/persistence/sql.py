from __future__ import annotations

from collections.abc import Sequence
from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import Project
from controlplane.domain.errors import AlreadyExists
from controlplane.domain.states import ProjectStatus
from controlplane.persistence.models import AuditEventRow, ProjectRow

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

    def list(self, *, limit: int, offset: int) -> Sequence[Project]:
        rows = self._s.scalars(
            select(ProjectRow)
            .order_by(ProjectRow.created_at, ProjectRow.id)
            .limit(limit)
            .offset(offset)
        )
        return [_project(r) for r in rows]


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
