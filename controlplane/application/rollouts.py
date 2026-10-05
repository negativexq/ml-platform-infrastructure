"""Starting and aborting canary rollouts. Advancing them is the RolloutReconciler's job."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from controlplane.application.context import current_traceparent
from controlplane.application.deployments import (
    DeploymentService,
    check_gpus,
    held_gpus,
    lock_project,
)
from controlplane.application.identity import current_actor
from controlplane.application.jobs import resolve_project
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import (
    DEFAULT_STEPS,
    DEPLOYABLE,
    Rollout,
    RolloutGate,
    validate_steps,
)
from controlplane.domain.errors import AlreadyExists, Conflict, NotFound
from controlplane.domain.states import DeploymentStatus, RolloutStatus


@dataclass(frozen=True, slots=True)
class RolloutView:
    rollout: Rollout
    deployment_name: str
    model_name: str
    model_version: int

    @property
    def traffic(self) -> dict[int, int]:
        """Who is getting traffic right now. A rolled-back canary is at 0%."""
        r = self.rollout
        if r.status is RolloutStatus.SUCCEEDED:
            return {r.to_revision: 100, r.from_revision: 0}
        if r.status is RolloutStatus.ROLLED_BACK:
            return {r.from_revision: 100, r.to_revision: 0}
        return {r.from_revision: 100 - r.percent, r.to_revision: r.percent}


class RolloutService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        deployments: DeploymentService,
        clock: Clock = utc_now,
    ) -> None:
        self._uow_factory = uow_factory
        self._deployments = deployments
        self._clock = clock

    def start(
        self,
        project_ref: str,
        deployment_name: str,
        model_name: str,
        version: int,
        *,
        steps: Sequence[int] | None = None,
        gate: RolloutGate | None = None,
    ) -> RolloutView:
        """Begin shifting traffic to a model version. The stable revision keeps serving
        the rest, and nothing about the model's champion status changes until the
        rollout has fully succeeded."""
        chosen = validate_steps(tuple(steps) if steps else DEFAULT_STEPS)
        revision = self._deployments.ensure_revision(
            project_ref, deployment_name, model_name, version
        )
        with self._uow_factory() as uow:
            project = lock_project(uow, resolve_project(uow, project_ref))
            deployment = uow.deployments.get_by_name(project.id, deployment_name)
            assert deployment is not None
            deployment = uow.deployments.lock(deployment.id)
            assert deployment is not None
            if (
                deployment.status is not DeploymentStatus.READY
                or deployment.active_revision is None
            ):
                raise Conflict(
                    f"deployment {deployment_name!r} is {deployment.status.value}; "
                    "a canary needs a READY deployment with a stable revision"
                )
            check_gpus(uow, project, deployment, held_gpus(uow, deployment, revision))
            stable = deployment.active_revision
            if deployment.desired_revision != stable:
                raise Conflict("the deployment is still converging on its desired revision")
            if revision.revision == stable:
                raise Conflict(f"revision {stable} is already the stable revision")
            if uow.rollouts.get_active(deployment.id) is not None:
                raise Conflict("this deployment already has a rollout in progress")
            candidate = uow.model_versions.lock(revision.model_version_id)
            if candidate is None or candidate.status not in DEPLOYABLE:
                raise Conflict("model version is no longer deployable")
            if uow.rollouts.get_active_by_version(candidate.id) is not None:
                raise Conflict("this model version already has a rollout in progress")
            now = self._clock()
            rollout = Rollout(
                deployment_id=deployment.id,
                from_revision=stable,
                to_revision=revision.revision,
                model_version_id=revision.model_version_id,
                steps=chosen,
                gate=gate or RolloutGate(),
                traceparent=current_traceparent(),
                created_at=now,
                updated_at=now,
            )
            try:
                uow.rollouts.add(rollout)
            except AlreadyExists:
                raise Conflict(
                    "deployment or model version already has a rollout in progress"
                ) from None
            uow.audit.record(
                _event(
                    now,
                    "rollout.started",
                    rollout,
                    project.id,
                    from_revision=stable,
                    to_revision=revision.revision,
                    steps=list(chosen),
                )
            )
            uow.commit()
            return self._view(uow, rollout)

    def get(self, rollout_id: UUID) -> RolloutView:
        with self._uow_factory() as uow:
            rollout = uow.rollouts.get(rollout_id)
            if rollout is None:
                raise NotFound("rollout", rollout_id)
            return self._view(uow, rollout)

    def list(self, project_ref: str, deployment_name: str) -> Sequence[RolloutView]:
        with self._uow_factory() as uow:
            project = lock_project(uow, resolve_project(uow, project_ref))
            deployment = uow.deployments.get_by_name(project.id, deployment_name)
            if deployment is None:
                raise NotFound("deployment", deployment_name)
            return [self._view(uow, r) for r in uow.rollouts.list(deployment.id)]

    def abort(self, rollout_id: UUID) -> RolloutView:
        """Idempotent. Before any traffic moved it just ends; otherwise the reconciler
        returns all traffic to the stable revision."""
        with self._uow_factory() as uow:
            rollout = uow.rollouts.get(rollout_id)
            if rollout is None:
                raise NotFound("rollout", rollout_id)
            if rollout.is_terminal or rollout.abort_requested:
                return self._view(uow, rollout)
            now = self._clock()
            deployment = uow.deployments.get(rollout.deployment_id)
            assert deployment is not None
            if rollout.status is RolloutStatus.PENDING:
                updated = rollout.transition_to(
                    RolloutStatus.ROLLED_BACK, now, "aborted before any traffic moved"
                )
            else:
                updated = rollout.with_abort_requested(now)
            uow.rollouts.update(updated, expected_status=rollout.status)
            uow.audit.record(
                _event(
                    now,
                    "rollout.abort_requested",
                    updated,
                    deployment.project_id,
                    status=updated.status.value,
                )
            )
            uow.commit()
            return self._view(uow, updated)

    @staticmethod
    def _view(uow: UnitOfWork, rollout: Rollout) -> RolloutView:
        deployment = uow.deployments.get(rollout.deployment_id)
        version = uow.model_versions.get(rollout.model_version_id)
        assert deployment is not None and version is not None
        model = uow.models.get(version.model_id)
        assert model is not None
        return RolloutView(rollout, deployment.name, model.name, version.version)


def _event(
    now: datetime, action: str, rollout: Rollout, project_id: UUID, **payload: object
) -> AuditEvent:
    return AuditEvent(
        occurred_at=now,
        actor=current_actor(),
        action=action,
        entity_type="rollout",
        entity_id=rollout.id,
        project_id=project_id,
        payload=payload,
    )
