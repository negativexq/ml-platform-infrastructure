"""Drives canary rollouts, one gated step at a time.

    PENDING -> PROGRESSING (10% -> 25% -> 50% -> 100%) -> SUCCEEDED
                         \\-> ROLLED_BACK (gate failed / canary never loaded / aborted)

At every step the canary must be loaded and serving, and the gate (error rate, p95
latency, enough requests to judge) is evaluated on the *canary revision's own*
metrics. A breach, a canary that fails to load, or an abort returns all traffic to the
stable revision. Only when the last step passes does the rollout succeed, and in that
same transaction the model version becomes champion and the deployment's stable
revision moves forward: the champion never changes ahead of the evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from controlplane.application.deployments import serving_ref
from controlplane.application.models import promote_version
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.application.providers import (
    MetricsProvider,
    ServingProvider,
    ServingSpec,
    ServingState,
)
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import (
    Deployment,
    DeploymentRevision,
    Project,
    Rollout,
    Verdict,
    evaluate_gate,
)
from controlplane.domain.errors import Conflict, NotFound
from controlplane.domain.states import ModelStatus, RolloutStatus

SYSTEM = "reconciler"


@dataclass(frozen=True, slots=True)
class RolloutResult:
    rollout_id: UUID
    before: RolloutStatus
    after: RolloutStatus
    percent: int


@dataclass(frozen=True, slots=True)
class _Context:
    rollout: Rollout
    deployment: Deployment
    project: Project
    stable: DeploymentRevision
    canary: DeploymentRevision

    @property
    def ref(self) -> str:
        return serving_ref(self.project, self.deployment)


class RolloutReconciler:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        serving: ServingProvider,
        metrics: MetricsProvider,
        clock: Clock = utc_now,
    ) -> None:
        self._uow_factory = uow_factory
        self._serving = serving
        self._metrics = metrics
        self._clock = clock

    def reconcile_all(self) -> list[RolloutResult]:
        with self._uow_factory() as uow:
            ids = [r.id for r in uow.rollouts.list_active()]
        results = []
        for rollout_id in ids:
            try:
                results.append(self.reconcile(rollout_id))
            except Conflict:
                continue  # another writer moved it first; next pass picks it up
        return results

    def reconcile(self, rollout_id: UUID) -> RolloutResult:
        ctx = self._load(rollout_id)
        rollout = ctx.rollout
        before = rollout.status
        if rollout.is_terminal:
            return RolloutResult(rollout_id, before, before, rollout.percent)
        if rollout.abort_requested:
            return self._rollback(ctx, "aborted by user")

        if rollout.status is RolloutStatus.PENDING:
            self._apply(ctx, rollout.steps[0])
            moved = rollout.transition_to(RolloutStatus.PROGRESSING, self._clock())
            moved = moved.at_step(0, moved.updated_at)
            self._save(moved, before, "rollout.step_applied", ctx, percent=moved.percent)
            return RolloutResult(rollout_id, before, moved.status, moved.percent)

        return self._progress(ctx)

    # -- one pass over a PROGRESSING rollout ------------------------------------

    def _progress(self, ctx: _Context) -> RolloutResult:
        rollout = ctx.rollout
        now = self._clock()
        status = self._serving.get_status(ctx.ref)

        if status.state is ServingState.FAILED:
            return self._rollback(ctx, f"canary failed to load: {status.reason or 'unknown'}")
        if status.state is ServingState.ABSENT:
            # Recreating only the canary would hand it all the traffic (the stable
            # revision is gone with the resource). Restore stable instead.
            return self._rollback(ctx, "serving resource disappeared during the rollout")
        if status.deployed_revision != rollout.to_revision:
            return self._rollback(
                ctx,
                f"serving resource was changed to revision {status.deployed_revision} mid-rollout",
            )

        if (
            status.state is not ServingState.READY
            or rollout.to_revision not in status.ready_revisions
        ):
            waited = (now - rollout.updated_at).total_seconds()
            if waited > rollout.gate.ready_timeout_seconds:
                return self._rollback(
                    ctx, f"canary did not become ready within {rollout.gate.ready_timeout_seconds}s"
                )
            return self._same(ctx)

        if rollout.step_started_at is None:  # first moment this step has a live canary
            observing = rollout.observing_since(now)
            self._save(
                observing, rollout.status, "rollout.observing", ctx, percent=observing.percent
            )
            return self._same(ctx, observing)

        metrics = self._metrics.revision_metrics(
            ctx.ref, rollout.to_revision, status.backend_revisions.get(rollout.to_revision)
        )
        gate = evaluate_gate(
            rollout.gate,
            requests=metrics.requests,
            error_rate=metrics.error_rate,
            p95_latency_ms=metrics.p95_latency_ms,
            elapsed_seconds=(now - rollout.step_started_at).total_seconds(),
        )
        if gate.verdict is Verdict.FAIL:
            return self._rollback(ctx, gate.reason, metrics=_snapshot(metrics))
        if gate.verdict is Verdict.WAIT:
            return self._same(ctx)

        if rollout.current_step + 1 < len(rollout.steps):
            index = rollout.current_step + 1
            self._apply(ctx, rollout.steps[index])
            advanced = rollout.at_step(index, now)
            self._save(
                advanced,
                rollout.status,
                "rollout.step_applied",
                ctx,
                percent=advanced.percent,
                gate=gate.reason,
            )
            return self._same(ctx, advanced)
        return self._succeed(ctx, gate.reason)

    # -- outcomes ---------------------------------------------------------------

    def _succeed(self, ctx: _Context, reason: str) -> RolloutResult:
        """All traffic is on the canary and it held. One transaction: rollout done, the
        deployment's stable revision moves, the model becomes champion."""
        now = self._clock()
        rollout = ctx.rollout
        with self._uow_factory() as uow:
            done = rollout.transition_to(RolloutStatus.SUCCEEDED, now, reason)
            uow.rollouts.update(done, expected_status=rollout.status)
            deployment = uow.deployments.get(rollout.deployment_id)
            assert deployment is not None
            moved = deployment.with_desired(rollout.to_revision, now).with_active(
                rollout.to_revision, now
            )
            uow.deployments.update(moved, expected_status=deployment.status)
            promote_version(uow, rollout.model_version_id, now, via_rollout=str(rollout.id))
            uow.audit.record(
                self._event(
                    now,
                    "rollout.succeeded",
                    rollout,
                    ctx.project.id,
                    from_revision=rollout.from_revision,
                    to_revision=rollout.to_revision,
                )
            )
            uow.commit()
        return RolloutResult(rollout.id, rollout.status, RolloutStatus.SUCCEEDED, 100)

    def _rollback(self, ctx: _Context, reason: str, **evidence: object) -> RolloutResult:
        """Return everything to the stable revision. Serving is repaired first: if that
        fails the rollout stays PROGRESSING and is retried, never marked rolled back
        while the canary may still be taking traffic."""
        rollout = ctx.rollout
        self._serving.deploy(self._spec(ctx, ctx.stable, canary_percent=None))
        now = self._clock()
        with self._uow_factory() as uow:
            done = rollout.transition_to(RolloutStatus.ROLLED_BACK, now, reason)
            uow.rollouts.update(done, expected_status=rollout.status)
            version = uow.model_versions.get(rollout.model_version_id)
            if version is not None and version.status is ModelStatus.CANDIDATE:
                # It passed evaluation but failed in production: it must not be promoted.
                uow.model_versions.update(
                    version.transition_to(ModelStatus.REJECTED, now),
                    expected_status=ModelStatus.CANDIDATE,
                )
            uow.audit.record(
                self._event(
                    now,
                    "rollout.rolled_back",
                    rollout,
                    ctx.project.id,
                    reason=reason,
                    from_revision=rollout.from_revision,
                    to_revision=rollout.to_revision,
                    percent_when_stopped=rollout.percent,
                    **evidence,
                )
            )
            uow.commit()
        return RolloutResult(rollout.id, rollout.status, RolloutStatus.ROLLED_BACK, 0)

    # -- serving ----------------------------------------------------------------

    def _spec(
        self, ctx: _Context, revision: DeploymentRevision, *, canary_percent: int | None
    ) -> ServingSpec:
        return ServingSpec(
            name=ctx.deployment.name,
            namespace=ctx.project.namespace,
            model_uri=revision.model_uri,
            revision=revision.revision,
            labels={
                "mlp.io/project-id": str(ctx.project.id),
                "mlp.io/deployment": ctx.deployment.name,
                "mlp.io/revision": str(revision.revision),
            },
            canary_percent=canary_percent,
        )

    def _apply(self, ctx: _Context, percent: int) -> None:
        self._serving.deploy(
            self._spec(ctx, ctx.canary, canary_percent=None if percent >= 100 else percent)
        )

    # -- persistence ------------------------------------------------------------

    def _load(self, rollout_id: UUID) -> _Context:
        with self._uow_factory() as uow:
            rollout = uow.rollouts.get(rollout_id)
            if rollout is None:
                raise NotFound("rollout", rollout_id)
            deployment = uow.deployments.get(rollout.deployment_id)
            assert deployment is not None
            project = uow.projects.get(deployment.project_id)
            stable = uow.revisions.get(deployment.id, rollout.from_revision)
            canary = uow.revisions.get(deployment.id, rollout.to_revision)
        assert project is not None and stable is not None and canary is not None
        return _Context(rollout, deployment, project, stable, canary)

    def _save(
        self,
        updated: Rollout,
        expected: RolloutStatus,
        action: str,
        ctx: _Context,
        **payload: object,
    ) -> None:
        with self._uow_factory() as uow:
            uow.rollouts.update(updated, expected_status=expected)
            uow.audit.record(
                self._event(updated.updated_at, action, updated, ctx.project.id, **payload)
            )
            uow.commit()

    @staticmethod
    def _same(ctx: _Context, rollout: Rollout | None = None) -> RolloutResult:
        r = rollout or ctx.rollout
        return RolloutResult(r.id, ctx.rollout.status, r.status, r.percent)

    @staticmethod
    def _event(
        now: datetime, action: str, rollout: Rollout, project_id: UUID, **payload: object
    ) -> AuditEvent:
        return AuditEvent(
            occurred_at=now,
            actor=SYSTEM,
            action=action,
            entity_type="rollout",
            entity_id=rollout.id,
            project_id=project_id,
            payload={"status": rollout.status.value, **payload},
        )


def _snapshot(metrics: object) -> dict[str, object]:
    return {
        k: getattr(metrics, k)
        for k in ("requests", "error_rate", "p95_latency_ms", "requests_per_second")
    }
