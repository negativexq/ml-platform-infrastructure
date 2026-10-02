from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from controlplane.application.ports import UnitOfWork
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import Project
from controlplane.domain.errors import AlreadyExists, Conflict, NotFound

Clock = Callable[[], datetime]
UnitOfWorkFactory = Callable[[], UnitOfWork]

# Until authentication exists, the actor is not something a client may assert.
ANONYMOUS = "anonymous"


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CreateProject:
    name: str
    display_name: str | None = None
    description: str = ""


class ProjectService:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock = utc_now) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def create(self, cmd: CreateProject) -> tuple[Project, bool]:
        """Create a project. Returns `(project, created)`.

        A repeat of an identical request returns the existing project with
        `created=False` and writes nothing. The same name with different
        attributes is a Conflict, never a silent overwrite.
        """
        candidate = Project.create(
            name=cmd.name,
            display_name=cmd.display_name,
            description=cmd.description,
            now=self._clock(),
        )
        try:
            with self._uow_factory() as uow:
                existing = uow.projects.get_by_name(candidate.name)
                if existing is None:
                    uow.projects.add(candidate)
                    uow.audit.record(
                        AuditEvent(
                            occurred_at=candidate.created_at,
                            actor=ANONYMOUS,
                            action="project.created",
                            entity_type="project",
                            entity_id=candidate.id,
                            project_id=candidate.id,
                            payload={"name": candidate.name},
                        )
                    )
                    uow.commit()
                    return candidate, True
        except AlreadyExists:
            pass  # lost a race with an identical concurrent request: replay below
        return self._replay(candidate), False

    def _replay(self, candidate: Project) -> Project:
        with self._uow_factory() as uow:
            existing = uow.projects.get_by_name(candidate.name)
        if existing is None:
            raise NotFound("project", candidate.name)
        if (existing.display_name, existing.description) != (
            candidate.display_name,
            candidate.description,
        ):
            raise Conflict(f"project {candidate.name!r} already exists with different attributes")
        return existing

    def get(self, project_id: UUID) -> Project:
        with self._uow_factory() as uow:
            project = uow.projects.get(project_id)
        if project is None:
            raise NotFound("project", project_id)
        return project

    def list(self, *, limit: int = 50, offset: int = 0) -> Sequence[Project]:
        with self._uow_factory() as uow:
            return uow.projects.list(limit=limit, offset=offset)
