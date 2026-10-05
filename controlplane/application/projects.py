from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from controlplane.application.context import current_traceparent
from controlplane.application.identity import current_actor
from controlplane.application.ports import UnitOfWork
from controlplane.domain.access import Membership, ProjectRole
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import Project
from controlplane.domain.errors import AlreadyExists, Conflict, NotFound
from controlplane.domain.states import ProjectStatus

Clock = Callable[[], datetime]
UnitOfWorkFactory = Callable[[], UnitOfWork]


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

    def create(self, cmd: CreateProject, *, owner: str | None = None) -> tuple[Project, bool]:
        """Create a project. Returns `(project, created)`.

        A repeat of an identical request returns the existing project with
        `created=False` and writes nothing. The same name with different
        attributes is a Conflict, never a silent overwrite.

        `owner` (a member subject such as `user:alice`) becomes the project's first admin, in
        the same transaction, so a new project is never left without someone to manage it.
        """
        candidate = Project.create(
            name=cmd.name,
            display_name=cmd.display_name,
            description=cmd.description,
            now=self._clock(),
            traceparent=current_traceparent(),
        )
        try:
            with self._uow_factory() as uow:
                existing = uow.projects.get_by_name(candidate.name)
                if existing is None:
                    uow.projects.add(candidate)
                    uow.audit.record(
                        AuditEvent(
                            occurred_at=candidate.created_at,
                            actor=current_actor(),
                            action="project.created",
                            entity_type="project",
                            entity_id=candidate.id,
                            project_id=candidate.id,
                            payload={"name": candidate.name},
                        )
                    )
                    if owner is not None:
                        membership = Membership.create(
                            project_id=candidate.id,
                            subject=owner,
                            role=ProjectRole.ADMIN,
                            now=candidate.created_at,
                        )
                        uow.memberships.add(membership)
                        uow.audit.record(_membership_event(membership, "membership.granted"))
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

    def list(
        self, *, limit: int = 50, offset: int = 0, only: Collection[UUID] | None = None
    ) -> Sequence[Project]:
        """`only` limits the list to these projects (the ones a caller is a member of)."""
        with self._uow_factory() as uow:
            if only is None:
                return uow.projects.list(limit=limit, offset=offset)
            wanted = set(only)
            visible = [p for p in uow.projects.list(limit=10_000, offset=0) if p.id in wanted]
            return visible[offset : offset + limit]

    def set_gpu_quota(self, project_ref: str, gpus: int) -> tuple[Project, bool]:
        """How many GPUs the project's workloads may hold together (platform admins only).
        The reconciler applies it to the namespace's ResourceQuota. Lowering it below what
        is in use is refused: it would leave running LLMs over quota."""
        from controlplane.application.deployments import gpus_in_use, lock_project
        from controlplane.application.jobs import resolve_project

        with self._uow_factory() as uow:
            project = lock_project(uow, resolve_project(uow, project_ref))
            if project.gpu_quota == gpus:
                return project, False
            used = gpus_in_use(uow, project.id)
            if gpus < used:
                raise Conflict(
                    f"{used} GPUs are in use in {project.name!r}; stop or scale down LLM "
                    f"deployments before lowering the quota to {gpus}"
                )
            now = self._clock()
            updated = project.with_gpu_quota(gpus, now)
            uow.projects.update(updated, expected_status=project.status)
            uow.audit.record(
                AuditEvent(
                    occurred_at=now,
                    actor=current_actor(),
                    action="project.gpu_quota_changed",
                    entity_type="project",
                    entity_id=project.id,
                    project_id=project.id,
                    payload={"gpus": gpus, "from": project.gpu_quota},
                )
            )
            uow.commit()
            return updated, True

    def request_delete(self, project_id: UUID) -> Project:
        """Record the intent to delete. The reconciler performs the cleanup and
        moves the project to DELETED once the namespace is really gone."""
        with self._uow_factory() as uow:
            project = uow.projects.get(project_id)
            if project is None:
                raise NotFound("project", project_id)
            if project.status in (ProjectStatus.DELETING, ProjectStatus.DELETED):
                return project  # idempotent
            now = self._clock()
            deleting = project.transition_to(ProjectStatus.DELETING, now)
            uow.projects.update(deleting, expected_status=project.status)
            uow.audit.record(
                AuditEvent(
                    occurred_at=now,
                    actor=current_actor(),
                    action="project.delete_requested",
                    entity_type="project",
                    entity_id=project.id,
                    project_id=project.id,
                    payload={"from": project.status.value},
                )
            )
            uow.commit()
            return deleting


def _membership_event(
    m: Membership, action: str, previous: ProjectRole | None = None
) -> AuditEvent:
    payload: dict[str, object] = {"subject": m.subject, "role": m.role.value}
    if previous is not None:
        payload["from"] = previous.value
    return AuditEvent(
        occurred_at=m.updated_at,
        actor=current_actor(),
        action=action,
        entity_type="membership",
        entity_id=m.id,
        project_id=m.project_id,
        payload=payload,
    )
