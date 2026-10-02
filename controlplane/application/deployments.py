"""Deployments, immutable revisions and endpoints.

The database holds the intent (a deployment, its revisions, which one is desired).
Nothing here talks to the serving system on the write path: the
DeploymentReconciler makes the intent real and reports readiness back.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from controlplane.application.jobs import resolve_project
from controlplane.application.models import restore_champion
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import ANONYMOUS, Clock, UnitOfWorkFactory, utc_now
from controlplane.application.providers import ExperimentProvider, ServingProvider
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import (
    DEPLOYABLE,
    Deployment,
    DeploymentRevision,
    Endpoint,
    Project,
)
from controlplane.domain.errors import AlreadyExists, Conflict, NotFound
from controlplane.domain.states import (
    DeploymentStatus,
    EndpointStatus,
    ModelStatus,
    ProjectStatus,
)


def serving_ref(project: Project, deployment: Deployment) -> str:
    """Deterministic reference of the serving resource behind a deployment."""
    return f"{project.namespace}/{deployment.name}"


def _audit(
    now: datetime,
    action: str,
    entity_type: str,
    entity_id: UUID,
    project_id: UUID,
    **payload: object,
) -> AuditEvent:
    return AuditEvent(
        occurred_at=now,
        actor=ANONYMOUS,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        project_id=project_id,
        payload=payload,
    )


@dataclass(frozen=True, slots=True)
class RevisionView:
    revision: DeploymentRevision
    model_name: str
    model_version: int


@dataclass(frozen=True, slots=True)
class DeploymentView:
    deployment: Deployment
    endpoint: Endpoint
    revisions: Sequence[RevisionView]


class DeploymentService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock = utc_now,
        experiments: ExperimentProvider | None = None,
        serving: ServingProvider | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._experiments = experiments
        self._serving = serving

    # -- deployments ----------------------------------------------------------

    def create(self, project_ref: str, name: str) -> tuple[DeploymentView, bool]:
        """Create a deployment and its endpoint. Identical repeat -> existing."""
        try:
            return self._create(project_ref, name)
        except AlreadyExists:  # lost a race with an identical request
            return self._create(project_ref, name)

    def _create(self, project_ref: str, name: str) -> tuple[DeploymentView, bool]:
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            now = self._clock()
            deployment = Deployment.create(project_id=project.id, name=name, now=now)
            existing = uow.deployments.get_by_name(project.id, deployment.name)
            if existing is not None:
                return self._view(uow, existing), False
            if project.status is not ProjectStatus.READY:
                raise Conflict(
                    f"project {project.name!r} is {project.status.value}; "
                    "deployments need a READY project"
                )
            uow.deployments.add(deployment)
            uow.endpoints.add(
                Endpoint(
                    project_id=project.id,
                    deployment_id=deployment.id,
                    name=deployment.name,
                    created_at=now,
                    updated_at=now,
                )
            )
            uow.audit.record(
                _audit(
                    now,
                    "deployment.created",
                    "deployment",
                    deployment.id,
                    project.id,
                    name=deployment.name,
                )
            )
            uow.commit()
            return self._view(uow, deployment), True

    def get(self, project_ref: str, name: str) -> DeploymentView:
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            deployment = uow.deployments.get_by_name(project.id, name)
            if deployment is None:
                raise NotFound("deployment", name)
            return self._view(uow, deployment)

    def list(self, project_ref: str) -> Sequence[DeploymentView]:
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            return [self._view(uow, d) for d in uow.deployments.list(project.id)]

    # -- revisions ------------------------------------------------------------

    def deploy(
        self, project_ref: str, name: str, model_name: str, version: int
    ) -> tuple[DeploymentView, bool]:
        """Serve a model version. Only versions that passed evaluation qualify. The
        same version as the latest revision is a no-op; anything else is revision + 1.
        Earlier revisions are never modified."""
        for _ in range(3):  # a concurrent deploy can take our revision number
            try:
                return self._deploy(project_ref, name, model_name, version)
            except AlreadyExists:
                continue
        raise AlreadyExists("revision", name)

    def _deploy(
        self, project_ref: str, name: str, model_name: str, version: int
    ) -> tuple[DeploymentView, bool]:
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            deployment = uow.deployments.get_by_name(project.id, name)
            if deployment is None:
                raise NotFound("deployment", name)
            model = uow.models.get_by_name(project.id, model_name)
            if model is None:
                raise NotFound("model", model_name)
            mv = next((v for v in uow.model_versions.list(model.id) if v.version == version), None)
            if mv is None:
                raise NotFound("model version", f"{model_name}@{version}")
            if mv.status not in DEPLOYABLE:
                allowed = sorted(s.value for s in DEPLOYABLE)
                raise Conflict(
                    f"version {version} of {model_name!r} is {mv.status.value}; "
                    f"only {allowed} versions can be deployed"
                )
            revisions = uow.revisions.list(deployment.id)
            if revisions and revisions[-1].model_version_id == mv.id:
                return self._view(uow, deployment), False  # already the latest revision
            registry, ref = model.registry_name(project.name), mv.external_ref
        if self._experiments is None or ref is None:
            raise Conflict("no model registry is configured; cannot locate the model artifact")
        model_uri = self._experiments.model_artifact_uri(registry, ref)
        if model_uri is None:
            raise Conflict(f"the registry has no artifact for {model_name!r} version {version}")

        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            deployment = uow.deployments.get_by_name(project.id, name)
            assert deployment is not None
            now = self._clock()
            revision = DeploymentRevision(
                deployment_id=deployment.id,
                revision=uow.revisions.next_revision(deployment.id),
                model_version_id=mv.id,
                model_uri=model_uri,
                created_at=now,
            )
            uow.revisions.add(revision)
            updated = deployment.with_desired(revision.revision, now)
            if updated.status is not DeploymentStatus.DEPLOYING:
                updated = updated.transition_to(DeploymentStatus.DEPLOYING, now)
            uow.deployments.update(updated, expected_status=deployment.status)
            uow.audit.record(
                _audit(
                    now,
                    "deployment.revision_created",
                    "deployment",
                    deployment.id,
                    project.id,
                    revision=revision.revision,
                    model=model_name,
                    version=version,
                )
            )
            uow.commit()
            return self._view(uow, updated), True

    def ensure_revision(
        self, project_ref: str, name: str, model_name: str, version: int
    ) -> DeploymentRevision:
        """The revision for a model version, created if it does not exist yet, WITHOUT
        making it the desired one. A rollout uses this: the new revision is a canary and
        the stable revision stays desired until the rollout succeeds."""
        for _ in range(3):
            try:
                return self._ensure_revision(project_ref, name, model_name, version)
            except AlreadyExists:
                continue
        raise AlreadyExists("revision", name)

    def _ensure_revision(
        self, project_ref: str, name: str, model_name: str, version: int
    ) -> DeploymentRevision:
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            deployment = uow.deployments.get_by_name(project.id, name)
            if deployment is None:
                raise NotFound("deployment", name)
            model = uow.models.get_by_name(project.id, model_name)
            if model is None:
                raise NotFound("model", model_name)
            mv = next((v for v in uow.model_versions.list(model.id) if v.version == version), None)
            if mv is None:
                raise NotFound("model version", f"{model_name}@{version}")
            if mv.status not in DEPLOYABLE:
                allowed = sorted(s.value for s in DEPLOYABLE)
                raise Conflict(
                    f"version {version} of {model_name!r} is {mv.status.value}; "
                    f"only {allowed} versions can be deployed"
                )
            for existing in uow.revisions.list(deployment.id):
                if existing.model_version_id == mv.id:
                    return existing
            registry, ref = model.registry_name(project.name), mv.external_ref
        if self._experiments is None or ref is None:
            raise Conflict("no model registry is configured; cannot locate the model artifact")
        model_uri = self._experiments.model_artifact_uri(registry, ref)
        if model_uri is None:
            raise Conflict(f"the registry has no artifact for {model_name!r} version {version}")
        with self._uow_factory() as uow:
            now = self._clock()
            revision = DeploymentRevision(
                deployment_id=deployment.id,
                revision=uow.revisions.next_revision(deployment.id),
                model_version_id=mv.id,
                model_uri=model_uri,
                created_at=now,
            )
            uow.revisions.add(revision)
            uow.audit.record(
                _audit(
                    now,
                    "deployment.revision_created",
                    "deployment",
                    deployment.id,
                    deployment.project_id,
                    revision=revision.revision,
                    model=model_name,
                    version=version,
                    canary=True,
                )
            )
            uow.commit()
            return revision

    def rollback(
        self, project_ref: str, name: str, to_revision: int | None = None
    ) -> DeploymentView:
        """Serve an earlier revision again (default: the one before the active one).

        Revisions are immutable, so nothing is recreated: the desired revision simply
        points back. The model states follow so the platform never says "champion" about
        a model that is no longer serving: the version being left is archived and the
        one being restored becomes champion again. The audit trail records both."""
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            deployment = uow.deployments.get_by_name(project.id, name)
            if deployment is None:
                raise NotFound("deployment", name)
            if uow.rollouts.get_active(deployment.id) is not None:
                raise Conflict("a rollout is in progress; abort it instead of rolling back")
            active = deployment.active_revision
            if active is None:
                raise Conflict("nothing has been served yet; there is nothing to roll back")
            revisions = {r.revision: r for r in uow.revisions.list(deployment.id)}
            target = (
                to_revision
                if to_revision is not None
                else max((n for n in revisions if n < active), default=None)
            )
            if target is None or target not in revisions:
                raise Conflict("there is no earlier revision to roll back to")
            if target == active and deployment.desired_revision == active:
                return self._view(uow, deployment)  # already there: idempotent
            now = self._clock()
            leaving = uow.model_versions.get(revisions[active].model_version_id)
            restoring = uow.model_versions.get(revisions[target].model_version_id)
            assert leaving is not None and restoring is not None
            if restoring.status is ModelStatus.ARCHIVED:
                restore_champion(uow, restoring.id, now, rollback_of=str(leaving.id))
            elif restoring.status is not ModelStatus.CHAMPION:
                raise Conflict(
                    f"revision {target} serves a {restoring.status.value} model; "
                    "only a former or current champion can be rolled back to"
                )
            updated = deployment.with_desired(target, now)
            if updated.status is not DeploymentStatus.DEPLOYING:
                updated = updated.transition_to(DeploymentStatus.DEPLOYING, now)
            uow.deployments.update(updated, expected_status=deployment.status)
            uow.audit.record(
                _audit(
                    now,
                    "deployment.rolled_back",
                    "deployment",
                    deployment.id,
                    project.id,
                    from_revision=active,
                    to_revision=target,
                )
            )
            uow.commit()
            return self._view(uow, updated)

    # -- serving --------------------------------------------------------------

    def predict(self, project_ref: str, name: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Forward an inference request through the platform. Refused unless the
        endpoint is READY, so callers never hit a half-rolled-out deployment."""
        if self._serving is None:
            raise Conflict("no serving provider is configured")
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            endpoint = uow.endpoints.get_by_name(project.id, name)
            if endpoint is None:
                raise NotFound("endpoint", name)
            if endpoint.status is not EndpointStatus.READY:
                raise Conflict(f"endpoint {name!r} is {endpoint.status.value}, not READY")
            deployment = uow.deployments.get(endpoint.deployment_id)
            assert deployment is not None
            ref = serving_ref(project, deployment)
        return self._serving.predict(ref, payload)

    def get_endpoint(self, project_ref: str, name: str) -> Endpoint:
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            endpoint = uow.endpoints.get_by_name(project.id, name)
        if endpoint is None:
            raise NotFound("endpoint", name)
        return endpoint

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _view(uow: UnitOfWork, deployment: Deployment) -> DeploymentView:
        endpoint = uow.endpoints.get_by_deployment(deployment.id)
        assert endpoint is not None
        views = []
        for revision in uow.revisions.list(deployment.id):
            mv = uow.model_versions.get(revision.model_version_id)
            assert mv is not None
            model = uow.models.get(mv.model_id)
            assert model is not None
            views.append(RevisionView(revision, model.name, mv.version))
        return DeploymentView(deployment, endpoint, views)
