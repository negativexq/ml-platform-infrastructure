"""Drives the cluster toward the state the database says a project should be in.

    desired state -> DB -> reconciler -> Kubernetes -> observed state -> DB status

The reconciler is the only writer of a project's lifecycle status after
creation. Every status change is a compare-and-swap with an audit event in the
same transaction, so two reconcilers racing cannot both win.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from controlplane.application.namespaces import namespace_spec
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.application.providers import ClusterProvider, NamespaceState
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import Project
from controlplane.domain.errors import Conflict, DomainError, NotFound
from controlplane.domain.states import ProjectStatus

SYSTEM = "reconciler"


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    project_id: UUID
    before: ProjectStatus
    after: ProjectStatus
    changed: tuple[str, ...] = ()  # cluster resources created or modified


class ProjectReconciler:
    def __init__(
        self, uow_factory: UnitOfWorkFactory, cluster: ClusterProvider, clock: Clock = utc_now
    ) -> None:
        self._uow_factory = uow_factory
        self._cluster = cluster
        self._clock = clock

    def reconcile_all(self) -> list[ReconcileResult]:
        with self._uow_factory() as uow:
            ids = [p.id for p in uow.projects.list_reconcilable()]
        results = []
        for project_id in ids:
            try:
                results.append(self.reconcile(project_id))
            except Conflict:
                continue  # another reconciler moved it first; next pass picks it up
        return results

    def reconcile(self, project_id: UUID) -> ReconcileResult:
        project = self._load(project_id)
        before = project.status
        changed: tuple[str, ...] = ()

        if project.status is ProjectStatus.DELETED:
            return ReconcileResult(project_id, before, before)
        if project.status is ProjectStatus.DELETING:
            return ReconcileResult(project_id, before, self._finish_delete(project).status)

        if project.status is ProjectStatus.READY:
            observation = self._cluster.observe(namespace_spec(project))
            if observation.state is NamespaceState.PRESENT and not observation.drifted:
                return ReconcileResult(project_id, before, before)  # converged: write nothing
            reason = (
                "namespace is terminating"
                if observation.state is NamespaceState.TERMINATING
                else f"drifted: {', '.join(observation.drifted) or 'namespace missing'}"
            )
            project = self._move(project, ProjectStatus.DRIFTED, "project.drift_detected", reason)

        if project.status in (ProjectStatus.PENDING, ProjectStatus.DRIFTED, ProjectStatus.FAILED):
            project = self._move(project, ProjectStatus.PROVISIONING, "project.provisioning")

        if project.status is ProjectStatus.PROVISIONING:
            try:
                changed = self._cluster.apply(namespace_spec(project))
                self._verify(project)
            except DomainError as exc:
                project = self._move(project, ProjectStatus.FAILED, "project.failed", str(exc))
            except Exception as exc:  # noqa: BLE001 - any provisioning failure must surface as FAILED
                project = self._move(
                    project, ProjectStatus.FAILED, "project.failed", f"{type(exc).__name__}: {exc}"
                )
            else:
                project = self._move(
                    project,
                    ProjectStatus.READY,
                    "project.provisioned",
                    payload={"changed": list(changed)},
                )
        return ReconcileResult(project_id, before, project.status, changed)

    # -- helpers ----------------------------------------------------------

    def _verify(self, project: Project) -> None:
        """READY is earned by observation, never by a successful apply call alone."""
        observation = self._cluster.observe(namespace_spec(project))
        if observation.state is not NamespaceState.PRESENT or observation.drifted:
            raise Conflict(
                f"namespace not converged after apply: {observation.state.value}, "
                f"drifted={list(observation.drifted)}"
            )

    def _finish_delete(self, project: Project) -> Project:
        spec = namespace_spec(project)
        try:
            self._cluster.delete(spec.namespace, project.id)
            state = self._cluster.observe(spec).state
        except DomainError as exc:
            return self._note(project, str(exc))  # e.g. namespace not ours: stay DELETING
        if state is NamespaceState.ABSENT:
            return self._move(project, ProjectStatus.DELETED, "project.deleted")
        return project  # still terminating; check again next pass

    def _load(self, project_id: UUID) -> Project:
        with self._uow_factory() as uow:
            project = uow.projects.get(project_id)
        if project is None:
            raise NotFound("project", project_id)
        return project

    def _move(
        self,
        project: Project,
        status: ProjectStatus,
        action: str,
        reason: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> Project:
        now = self._clock()
        moved = project.transition_to(status, now, reason)
        self._write(project, moved, action, {"from": project.status.value, **(payload or {})})
        return moved

    def _note(self, project: Project, reason: str) -> Project:
        if project.status_reason == reason:
            return project
        now = self._clock()
        noted = project.with_reason(reason, now)
        self._write(project, noted, "project.delete_blocked", {})
        return noted

    def _write(
        self, before: Project, after: Project, action: str, payload: dict[str, object]
    ) -> None:
        with self._uow_factory() as uow:
            self._persist(uow, before, after, action, payload)
            uow.commit()

    def _persist(
        self,
        uow: UnitOfWork,
        before: Project,
        after: Project,
        action: str,
        payload: dict[str, object],
    ) -> None:
        uow.projects.update(after, expected_status=before.status)
        uow.audit.record(
            AuditEvent(
                occurred_at=after.updated_at,
                actor=SYSTEM,
                action=action,
                entity_type="project",
                entity_id=after.id,
                project_id=after.id,
                payload={"to": after.status.value, "reason": after.status_reason, **payload},
            )
        )
