"""Makes the desired revision of every deployment real, and keeps it real.

    desired revision (DB) -> reconciler -> serving system -> observed readiness -> DB

READY is earned by observation: the serving system must report the *desired*
revision as loaded and serving, and the endpoint is marked READY in the same
transaction, so a deployment is never READY while its endpoint is not.

If a READY deployment's serving resource disappears or stops being ready, the
deployment goes DEGRADED (endpoint UNAVAILABLE), is recreated from the immutable
revision, and returns to READY only once it is observed serving again.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from uuid import UUID

from controlplane.application.deployments import serving_ref
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.application.providers import (
    ServingProvider,
    ServingSpec,
    ServingState,
    ServingStatus,
)
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import Deployment, Endpoint
from controlplane.domain.errors import NotFound
from controlplane.domain.states import DeploymentStatus, EndpointStatus
from controlplane.reconciliation.batch import ReconcileBackoff, reconcile_batch

SYSTEM = "reconciler"


@dataclass(frozen=True, slots=True)
class DeploymentResult:
    deployment_id: UUID
    before: DeploymentStatus
    after: DeploymentStatus
    applied: bool = False  # the serving resource was created or updated this pass


class DeploymentReconciler:
    def __init__(
        self, uow_factory: UnitOfWorkFactory, serving: ServingProvider, clock: Clock = utc_now
    ) -> None:
        self._uow_factory = uow_factory
        self._serving = serving
        self._clock = clock
        self._retry = ReconcileBackoff()

    def reconcile_all(self) -> list[DeploymentResult]:
        with self._uow_factory() as uow:
            ids = [d.id for d in uow.deployments.list_reconcilable()]
        return reconcile_batch(ids, self.reconcile, "deployments", self._retry)

    def reconcile(self, deployment_id: UUID) -> DeploymentResult:
        with self._uow_factory() as uow:
            deployment = uow.deployments.get(deployment_id)
            if deployment is None:
                raise NotFound("deployment", deployment_id)
            project = uow.projects.get(deployment.project_id)
            endpoint = uow.endpoints.get_by_deployment(deployment_id)
            rolling_out = uow.rollouts.get_active(deployment_id) is not None
            revision = (
                uow.revisions.get(deployment_id, deployment.desired_revision)
                if deployment.desired_revision is not None
                else None
            )
        before = deployment.status
        if before is DeploymentStatus.DELETED:
            return DeploymentResult(deployment_id, before, before)
        if before is DeploymentStatus.DELETING and project is not None:
            ref = serving_ref(project, deployment)
            self._serving.delete(ref)
            if self._serving.get_status(ref).state is not ServingState.ABSENT:
                return DeploymentResult(deployment_id, before, before)
            now = self._clock()
            done = replace(
                deployment.transition_to(DeploymentStatus.DELETED, now),
                active_revision=None,
                desired_revision=None,
            )
            with self._uow_factory() as uow:
                uow.deployments.update(done, expected_status=before)
                uow.audit.record(
                    self._event(now, "deployment.deleted", "deployment", deployment_id, deployment)
                )
                uow.commit()
            return DeploymentResult(deployment_id, before, done.status)
        if rolling_out:
            # The rollout reconciler owns the serving resource (a canary split looks like
            # drift to this one). It resumes the moment the rollout ends.
            return DeploymentResult(deployment_id, before, before)
        if project is None or endpoint is None or revision is None:
            return DeploymentResult(deployment_id, before, before)
        if deployment.status is DeploymentStatus.FAILED:
            return DeploymentResult(deployment_id, before, before)  # a new revision retries it

        ref = serving_ref(project, deployment)
        desired = revision.revision
        status = self._serving.get_status(ref)

        spec = ServingSpec(
            name=deployment.name,
            namespace=project.namespace,
            model_uri=revision.model_uri,
            revision=desired,
            runtime=revision.runtime.value,
            gpus=revision.gpus,
            context_length=revision.context_length,
            min_scale=revision.min_scale,
            max_scale=revision.max_scale,
            function=revision.function.to_json() if revision.function else None,
            secret_refs=revision.secret_refs,
            labels={
                "mlp.io/project-id": str(project.id),
                "mlp.io/deployment": deployment.name,
                "mlp.io/revision": str(desired),
            },
        )
        drifted = status.state is not ServingState.ABSENT and not self._serving.matches(spec)
        if deployment.status is DeploymentStatus.READY:
            if self._serving_it(status, desired) and not drifted:
                return DeploymentResult(deployment_id, before, before)  # converged: no writes
            reason = (
                "owned serving configuration differs"
                if drifted
                else "serving resource is missing"
                if status.state is ServingState.ABSENT
                else f"serving resource is not ready ({status.state.value})"
            )
            deployment, endpoint = self._degrade(deployment, endpoint, reason)

        if deployment.status is DeploymentStatus.DEGRADED:
            deployment = self._move(
                deployment, DeploymentStatus.DEPLOYING, "deployment.redeploying"
            )

        applied = False
        if status.state is ServingState.ABSENT or status.deployed_revision != desired or drifted:
            self._serving.deploy(spec)
            applied = True
            status = self._serving.get_status(ref)

        if self._serving_it(status, desired):
            deployment = self._mark_ready(deployment, endpoint, desired, status)
        elif status.state is ServingState.FAILED:
            deployment, endpoint = self._fail(
                deployment, endpoint, status.reason or "serving failed"
            )
        return DeploymentResult(deployment_id, before, deployment.status, applied)

    @staticmethod
    def _serving_it(status: ServingStatus, revision: int) -> bool:
        return (
            status.state is ServingState.READY
            and status.deployed_revision == revision
            and revision in status.ready_revisions
        )

    # -- transitions (each one transaction with its audit event) --------------

    def _mark_ready(
        self, deployment: Deployment, endpoint: Endpoint, revision: int, status: ServingStatus
    ) -> Deployment:
        now = self._clock()
        with self._uow_factory() as uow:
            if endpoint.status is not EndpointStatus.READY:
                ready = endpoint.transition_to(EndpointStatus.READY, now, url=status.url)
                uow.endpoints.update(ready, expected_status=endpoint.status)
                uow.audit.record(
                    self._event(now, "endpoint.ready", "endpoint", endpoint.id, deployment)
                )
            done = deployment.transition_to(DeploymentStatus.READY, now).with_active(revision, now)
            uow.deployments.update(done, expected_status=deployment.status)
            uow.audit.record(
                self._event(
                    now,
                    "deployment.ready",
                    "deployment",
                    deployment.id,
                    deployment,
                    revision=revision,
                )
            )
            uow.commit()
        return done

    def _degrade(
        self, deployment: Deployment, endpoint: Endpoint, reason: str
    ) -> tuple[Deployment, Endpoint]:
        now = self._clock()
        with self._uow_factory() as uow:
            down = endpoint.transition_to(EndpointStatus.UNAVAILABLE, now)
            uow.endpoints.update(down, expected_status=endpoint.status)
            degraded = deployment.transition_to(DeploymentStatus.DEGRADED, now, reason)
            uow.deployments.update(degraded, expected_status=deployment.status)
            uow.audit.record(
                self._event(
                    now,
                    "deployment.drift_detected",
                    "deployment",
                    deployment.id,
                    deployment,
                    reason=reason,
                )
            )
            uow.commit()
        return degraded, down

    def _fail(
        self, deployment: Deployment, endpoint: Endpoint, reason: str
    ) -> tuple[Deployment, Endpoint]:
        now = self._clock()
        with self._uow_factory() as uow:
            if endpoint.status is EndpointStatus.READY:
                endpoint = endpoint.transition_to(EndpointStatus.UNAVAILABLE, now)
                uow.endpoints.update(endpoint, expected_status=EndpointStatus.READY)
            failed = deployment.transition_to(DeploymentStatus.FAILED, now, reason)
            uow.deployments.update(failed, expected_status=deployment.status)
            uow.audit.record(
                self._event(
                    now, "deployment.failed", "deployment", deployment.id, deployment, reason=reason
                )
            )
            uow.commit()
        return failed, endpoint

    def _move(self, deployment: Deployment, status: DeploymentStatus, action: str) -> Deployment:
        now = self._clock()
        moved = deployment.transition_to(status, now)
        with self._uow_factory() as uow:
            uow.deployments.update(moved, expected_status=deployment.status)
            uow.audit.record(self._event(now, action, "deployment", deployment.id, deployment))
            uow.commit()
        return moved

    @staticmethod
    def _event(
        now: datetime,
        action: str,
        entity_type: str,
        entity_id: UUID,
        deployment: Deployment,
        **payload: object,
    ) -> AuditEvent:
        return AuditEvent(
            occurred_at=now,
            actor=SYSTEM,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            project_id=deployment.project_id,
            payload=payload,
        )
