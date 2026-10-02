from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import ANONYMOUS, Clock, UnitOfWorkFactory, utc_now
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import JobDefinition, Project
from controlplane.domain.errors import AlreadyExists, Conflict, NotFound


@dataclass(frozen=True, slots=True)
class CreateJob:
    name: str
    image: str
    command: tuple[str, ...] = ()
    resources: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)


def resolve_project(uow: UnitOfWork, ref: str) -> Project:
    """A project is addressed by UUID or by name."""
    try:
        project = uow.projects.get(UUID(ref))
    except ValueError:
        project = uow.projects.get_by_name(ref)
    if project is None:
        raise NotFound("project", ref)
    return project


class JobService:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock = utc_now) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def create(self, project_ref: str, cmd: CreateJob) -> tuple[JobDefinition, bool]:
        """Create a job definition. Identical repeat -> existing, `created=False`;
        same name with different content -> Conflict (definitions are immutable)."""
        try:
            with self._uow_factory() as uow:
                project = resolve_project(uow, project_ref)
                candidate = JobDefinition.create(
                    project_id=project.id,
                    name=cmd.name,
                    image=cmd.image,
                    command=cmd.command,
                    resources=cmd.resources,
                    env=cmd.env,
                    now=self._clock(),
                )
                existing = uow.jobs.get_by_name(project.id, candidate.name)
                if existing is None:
                    uow.jobs.add(candidate)
                    uow.audit.record(
                        AuditEvent(
                            occurred_at=candidate.created_at,
                            actor=ANONYMOUS,
                            action="job.created",
                            entity_type="job",
                            entity_id=candidate.id,
                            project_id=project.id,
                            payload={"name": candidate.name, "image": candidate.image},
                        )
                    )
                    uow.commit()
                    return candidate, True
                return self._same_or_conflict(existing, candidate), False
        except AlreadyExists:
            with self._uow_factory() as uow:
                project = resolve_project(uow, project_ref)
                existing = uow.jobs.get_by_name(project.id, cmd.name)
            if existing is None:
                raise
            return self._same_or_conflict(existing, candidate), False

    @staticmethod
    def _same_or_conflict(existing: JobDefinition, candidate: JobDefinition) -> JobDefinition:
        same = (existing.image, existing.command, dict(existing.resources), dict(existing.env)) == (
            candidate.image,
            candidate.command,
            dict(candidate.resources),
            dict(candidate.env),
        )
        if not same:
            raise Conflict(
                f"job {candidate.name!r} already exists with a different definition; "
                "definitions are immutable, create a new name"
            )
        return existing

    def get(self, project_ref: str, name: str) -> JobDefinition:
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            job = uow.jobs.get_by_name(project.id, name)
        if job is None:
            raise NotFound("job", name)
        return job

    def list(self, project_ref: str) -> Sequence[JobDefinition]:
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            return uow.jobs.list(project.id)
