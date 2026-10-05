"""Project membership: who may do what in a project."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from controlplane.application.jobs import resolve_project
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import Clock, UnitOfWorkFactory, _membership_event, utc_now
from controlplane.domain.access import Membership, ProjectRole, validate_subject
from controlplane.domain.errors import Conflict, NotFound


class MembershipService:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock = utc_now) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def list(self, project_ref: str) -> Sequence[Membership]:
        with self._uow_factory() as uow:
            return uow.memberships.list(resolve_project(uow, project_ref).id)

    def set_role(
        self, project_ref: str, subject: str, role: ProjectRole
    ) -> tuple[Membership, bool]:
        """Grant a role, or change an existing member's. Returns `(membership, changed)`;
        setting the role someone already has writes nothing."""
        validate_subject(subject)
        role = ProjectRole(role)
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            if uow.projects.lock(project.id) is None:
                raise NotFound("project", project.id)
            existing = uow.memberships.get(project.id, subject)
            if existing is None:
                created = Membership.create(
                    project_id=project.id, subject=subject, role=role, now=self._clock()
                )
                uow.memberships.add(created)
                uow.audit.record(_membership_event(created, "membership.granted"))
                uow.commit()
                return created, True
            if existing.role is role:
                return existing, False
            if existing.role is ProjectRole.ADMIN:
                _keep_an_admin(uow, existing)
            updated = replace(existing, role=role, updated_at=self._clock())
            uow.memberships.update(updated)
            uow.audit.record(_membership_event(updated, "membership.changed", existing.role))
            uow.commit()
            return updated, True

    def remove(self, project_ref: str, subject: str) -> None:
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            if uow.projects.lock(project.id) is None:
                raise NotFound("project", project.id)
            existing = uow.memberships.get(project.id, subject)
            if existing is None:
                raise NotFound("member", subject)
            if existing.role is ProjectRole.ADMIN:
                _keep_an_admin(uow, existing)
            uow.memberships.remove(project.id, subject)
            uow.audit.record(
                _membership_event(replace(existing, updated_at=self._clock()), "membership.revoked")
            )
            uow.commit()


def _keep_an_admin(uow: UnitOfWork, leaving: Membership) -> None:
    others = [
        m
        for m in uow.memberships.list(leaving.project_id)
        if m.role is ProjectRole.ADMIN and m.id != leaving.id
    ]
    if not others:
        raise Conflict(
            f"{leaving.subject} is the project's last admin; make someone else admin first"
        )
