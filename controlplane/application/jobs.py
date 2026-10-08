from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from controlplane.application.identity import current_actor
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.application.secrets import SecretProvider, validate_refs
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import JobDefinition, Project
from controlplane.domain.errors import AlreadyExists, Conflict, InvalidArgument, NotFound
from controlplane.domain.secrets import SecretRefs


@dataclass(frozen=True, slots=True)
class CreateJob:
    name: str
    image: str
    command: tuple[str, ...] = ()
    resources: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    secret_refs: SecretRefs = field(default_factory=SecretRefs)
    parameter_schema: Mapping[str, Any] = field(default_factory=dict)
    timeout_seconds: int = 3600


def resolve_project(uow: UnitOfWork, ref: str) -> Project:
    """A project is addressed by UUID or by name."""
    try:
        project = uow.projects.get(UUID(ref))
    except ValueError:
        project = uow.projects.get_by_name(ref)
    if project is None:
        raise NotFound("project", ref)
    return project


def require_training_digest(image: str) -> None:
    if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image):
        raise InvalidArgument("training image requires a full immutable @sha256 digest")


class JobService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock = utc_now,
        secrets: SecretProvider | None = None,
        *,
        require_image_digest: bool = False,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._secrets = secrets
        self._require_image_digest = require_image_digest

    def create(self, project_ref: str, cmd: CreateJob) -> tuple[JobDefinition, bool]:
        """Create a job definition. Identical repeat -> existing, `created=False`;
        same name with different content -> Conflict (definitions are immutable)."""
        if self._require_image_digest:
            require_training_digest(cmd.image)
        if cmd.secret_refs.storage_secret:
            raise InvalidArgument("storage credentials apply only to classic serving models")
        try:
            with self._uow_factory() as uow:
                project = resolve_project(uow, project_ref)
                locked_project = uow.projects.lock(project.id)
                assert locked_project is not None
                project = locked_project
                validate_refs(self._secrets, project, cmd.secret_refs, cmd.env)
                candidate = JobDefinition.create(
                    project_id=project.id,
                    name=cmd.name,
                    image=cmd.image,
                    command=cmd.command,
                    resources=cmd.resources,
                    env=cmd.env,
                    now=self._clock(),
                    timeout_seconds=cmd.timeout_seconds,
                    secret_refs=cmd.secret_refs,
                    parameter_schema=cmd.parameter_schema,
                )
                existing = uow.jobs.get_by_name(project.id, candidate.name)
                if existing is None:
                    uow.jobs.add(candidate)
                    uow.audit.record(
                        AuditEvent(
                            occurred_at=candidate.created_at,
                            actor=current_actor(),
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
        same = same and existing.timeout_seconds == candidate.timeout_seconds
        same = same and existing.secret_refs == candidate.secret_refs
        same = same and existing.parameter_schema == candidate.parameter_schema
        same = same and existing.batch_spec == candidate.batch_spec
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
