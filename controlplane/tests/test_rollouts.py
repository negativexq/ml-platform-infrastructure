"""M19 skeleton tests: gates, canary rollout, rollback, safety."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from controlplane.adapters.fakes import (
    FakeClusterProvider,
    FakeExperimentProvider,
    FakeMetricsProvider,
    FakeServingProvider,
)
from controlplane.api.app import create_app
from controlplane.application.deployments import DeploymentService
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import CreateProject, ProjectService
from controlplane.application.providers import RegisteredVersion, RevisionMetrics
from controlplane.application.rollouts import RolloutService
from controlplane.domain.entities import (
    Model,
    ModelVersion,
    RolloutGate,
    Verdict,
    evaluate_gate,
    validate_steps,
)
from controlplane.domain.errors import Conflict, InvalidArgument
from controlplane.domain.states import DeploymentStatus, ModelStatus, RolloutStatus
from controlplane.reconciliation.deployments import DeploymentReconciler
from controlplane.reconciliation.projects import ProjectReconciler
from controlplane.reconciliation.rollouts import RolloutReconciler

Factory = Callable[[], UnitOfWork]
REGISTRY = "credit-risk-scorer"
REF = "mlp-credit-risk/credit-risk-prod"
GATE = RolloutGate(max_error_rate=0.01, max_p95_latency_ms=300, min_requests=20, step_seconds=60)
GOOD = RevisionMetrics(p95_latency_ms=120.0, error_rate=0.0, requests_per_second=5.0, requests=100)


# -- the gate decision (pure) ---------------------------------------------------


def judge(**kw: Any) -> Verdict:
    base: dict[str, Any] = {
        "requests": 100.0,
        "error_rate": 0.0,
        "p95_latency_ms": 100.0,
        "elapsed_seconds": 120.0,
    }
    return evaluate_gate(GATE, **{**base, **kw}).verdict


def test_gate_passes_only_after_observing_clean_traffic() -> None:
    assert judge() is Verdict.PASS
    assert judge(elapsed_seconds=10) is Verdict.WAIT


def test_a_breach_fails_immediately_once_there_is_enough_traffic() -> None:
    assert judge(error_rate=0.05, elapsed_seconds=1) is Verdict.FAIL
    assert judge(p95_latency_ms=900, elapsed_seconds=1) is Verdict.FAIL
    # a breach on too little traffic is not proof yet
    assert judge(error_rate=1.0, requests=3, elapsed_seconds=1) is Verdict.WAIT


def test_the_gate_fails_closed_without_evidence() -> None:
    assert judge(requests=5, elapsed_seconds=120) is Verdict.FAIL  # nobody exercised it
    assert judge(requests=None, error_rate=None, p95_latency_ms=None) is Verdict.FAIL
    assert judge(error_rate=None) is Verdict.FAIL  # traffic seen but no error rate


def test_limits_are_inclusive() -> None:
    assert judge(error_rate=0.01, p95_latency_ms=300) is Verdict.PASS


def test_gate_and_step_validation() -> None:
    for bad in ({"max_error_rate": 2}, {"max_p95_latency_ms": 0}, {"min_requests": 0}):
        with pytest.raises(InvalidArgument):
            RolloutGate(**bad)
    assert validate_steps((10, 50, 100)) == (10, 50, 100)
    for steps in ((), (10, 50), (50, 10, 100), (0, 100), (10, 10, 100), (10, 101)):
        with pytest.raises(InvalidArgument):
            validate_steps(steps)


# -- fixtures -------------------------------------------------------------------


class ManualClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(milliseconds=1)  # strictly increasing, effectively frozen
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Env:
    def __init__(self, factory: Factory) -> None:
        self.factory = factory
        self.clock = ManualClock()
        self.experiments = FakeExperimentProvider()
        self.serving = FakeServingProvider()
        self.metrics = FakeMetricsProvider()
        self.deployments = DeploymentService(factory, self.clock, self.experiments, self.serving)
        self.rollouts = RolloutService(factory, self.deployments, self.clock)
        self.rollout_rec = RolloutReconciler(factory, self.serving, self.metrics, self.clock)
        self.deploy_rec = DeploymentReconciler(factory, self.serving, self.clock)
        project, _ = ProjectService(factory, self.clock).create(CreateProject(name="credit-risk"))
        ProjectReconciler(factory, FakeClusterProvider(), self.clock).reconcile(project.id)
        self.project_id = project.id
        with factory() as uow:
            self.model = Model.create(
                project_id=project.id, name="scorer", thresholds={}, now=self.clock()
            )
            uow.models.add(self.model)
            uow.commit()
        for status in (
            ModelStatus.CHAMPION,  # v1: serving
            ModelStatus.CANDIDATE,  # v2
            ModelStatus.CANDIDATE,  # v3
            ModelStatus.REGISTERED,  # v4
        ):
            self.version(status)
        self.deployments.create("credit-risk", "credit-risk-prod")
        self.deployments.deploy("credit-risk", "credit-risk-prod", "scorer", 1)
        self.deploy_rec.reconcile(self.deployment_id())
        self.metrics.by_revision[(REF, 2)] = GOOD
        self.metrics.by_revision[(REF, 3)] = GOOD

    def version(self, status: ModelStatus) -> None:
        with self.factory() as uow:
            n = uow.model_versions.next_version(self.model.id)
            now = self.clock()
            uow.model_versions.add(
                ModelVersion(
                    model_id=self.model.id,
                    version=n,
                    status=status,
                    external_ref=str(n),
                    created_at=now,
                    updated_at=now,
                )
            )
            uow.commit()
        self.experiments.registered.setdefault(REGISTRY, []).append(RegisteredVersion(str(n), None))

    def deployment_id(self) -> UUID:
        return self.deployments.get("credit-risk", "credit-risk-prod").deployment.id

    def start(self, version: int = 2, gate: RolloutGate = GATE, **kw: Any) -> UUID:
        view = self.rollouts.start(
            "credit-risk", "credit-risk-prod", "scorer", version, gate=gate, **kw
        )
        self.clock.advance(1)
        return view.rollout.id

    def reconcile(self, rollout_id: UUID) -> Any:
        return self.rollout_rec.reconcile(rollout_id)

    def run_step(self, rollout_id: UUID) -> Any:
        """One full step: let it start observing, let the observation time pass, judge."""
        self.reconcile(rollout_id)
        self.clock.advance(61)
        return self.reconcile(rollout_id)

    def rollout(self, rollout_id: UUID) -> Any:
        return self.rollouts.get(rollout_id)

    def status(self, version: int) -> ModelStatus:
        with self.factory() as uow:
            return next(
                v.status for v in uow.model_versions.list(self.model.id) if v.version == version
            )

    def deployment(self) -> Any:
        return self.deployments.get("credit-risk", "credit-risk-prod").deployment

    def audit(self, **kw: Any) -> list[str]:
        with self.factory() as uow:
            return [e.action for e in uow.audit.list(**kw)]


@pytest.fixture
def env(uow_factory: Factory) -> Env:
    return Env(uow_factory)


# -- starting -------------------------------------------------------------------


def test_starting_records_intent_and_touches_nothing(env: Env) -> None:
    calls = env.serving.deploy_calls
    rid = env.start()
    view = env.rollout(rid)
    assert view.rollout.status is RolloutStatus.PENDING and view.traffic == {1: 100, 2: 0}
    assert env.serving.deploy_calls == calls  # no traffic moved
    dep = env.deployment()
    assert (dep.desired_revision, dep.active_revision) == (1, 1)  # stable is still revision 1
    revisions = env.deployments.get("credit-risk", "credit-risk-prod").revisions
    assert [r.revision.revision for r in revisions] == [1, 2]  # canary revision exists


def test_preconditions(env: Env) -> None:
    env.start()
    with pytest.raises(Conflict, match="already has a rollout"):
        env.start(3)
    with pytest.raises(Conflict, match="only"):
        env.rollouts.start("credit-risk", "credit-risk-prod", "scorer", 4)  # REGISTERED


def test_the_stable_version_cannot_be_rolled_out_onto_itself(env: Env) -> None:
    with pytest.raises(Conflict, match="already the stable"):
        env.rollouts.start("credit-risk", "credit-risk-prod", "scorer", 1)


def test_a_deployment_that_is_not_ready_cannot_canary(env: Env) -> None:
    env.serving.delete(REF)
    env.serving.auto_ready = False
    env.deploy_rec.reconcile(env.deployment_id())  # DEGRADED -> DEPLOYING, not ready yet
    assert env.deployment().status is DeploymentStatus.DEPLOYING
    with pytest.raises(Conflict, match="READY"):
        env.rollouts.start("credit-risk", "credit-risk-prod", "scorer", 2)


# -- the happy path -------------------------------------------------------------


def test_canary_steps_through_the_split_and_promotes_only_at_the_end(env: Env) -> None:
    rid = env.start()
    assert env.reconcile(rid).percent == 10
    assert env.serving.split(REF) == {2: 10, 1: 90}

    for expected_split in ({2: 25, 1: 75}, {2: 50, 1: 50}, {2: 100}):
        result = env.run_step(rid)
        assert result.after is RolloutStatus.PROGRESSING
        assert env.serving.split(REF) == expected_split
        # the evidence is not complete yet: nothing about champions has changed
        assert (env.status(1), env.status(2)) == (ModelStatus.CHAMPION, ModelStatus.CANDIDATE)
        assert env.deployment().active_revision == 1

    assert env.run_step(rid).after is RolloutStatus.SUCCEEDED
    assert (env.status(1), env.status(2)) == (ModelStatus.ARCHIVED, ModelStatus.CHAMPION)
    dep = env.deployment()
    assert (dep.desired_revision, dep.active_revision) == (2, 2)
    assert env.rollout(rid).traffic == {2: 100, 1: 0}
    assert {"rollout.started", "rollout.succeeded", "model_version.promoted"} <= set(
        env.audit(project_id=env.project_id)
    )


def test_each_step_is_judged_on_the_canarys_own_metrics(env: Env) -> None:
    rid = env.start()
    env.reconcile(rid)
    env.metrics.by_revision[(REF, 1)] = RevisionMetrics(9999, 1.0, 1.0, 1000)  # stable is awful
    assert env.run_step(rid).after is RolloutStatus.PROGRESSING  # irrelevant to the canary


# -- the failure drill ----------------------------------------------------------


def test_error_injection_rolls_back_to_the_old_champion(env: Env) -> None:
    rid = env.start()
    env.reconcile(rid)  # 10% canary
    assert env.serving.split(REF) == {2: 10, 1: 90}
    env.metrics.by_revision[(REF, 2)] = RevisionMetrics(120, 0.5, 5.0, 100)  # injected 500s

    env.reconcile(rid)  # starts observing
    result = env.reconcile(rid)  # breach: fails at once, even before the step time elapsed
    assert result.after is RolloutStatus.ROLLED_BACK and result.percent == 0

    assert env.serving.split(REF) == {1: 100}  # all traffic back on the old champion
    view = env.rollout(rid)
    assert view.traffic == {1: 100, 2: 0}
    assert "error rate 50.00%" in (view.rollout.status_reason or "")
    assert (env.status(1), env.status(2)) == (ModelStatus.CHAMPION, ModelStatus.REJECTED)
    dep = env.deployment()
    assert (dep.desired_revision, dep.active_revision) == (1, 1)  # champion did not change
    assert "rollout.rolled_back" in env.audit(entity_id=rid)

    # and the deployment converges back to a healthy single revision
    assert env.deploy_rec.reconcile(env.deployment_id()).after is DeploymentStatus.READY


def test_a_rejected_canary_cannot_be_rolled_out_again(env: Env) -> None:
    rid = env.start()
    env.reconcile(rid)
    env.metrics.by_revision[(REF, 2)] = RevisionMetrics(120, 0.5, 5.0, 100)
    env.reconcile(rid)
    env.reconcile(rid)
    with pytest.raises(Conflict, match="only"):
        env.rollouts.start("credit-risk", "credit-risk-prod", "scorer", 2)


def test_latency_breach_rolls_back(env: Env) -> None:
    rid = env.start()
    env.reconcile(rid)
    env.metrics.by_revision[(REF, 2)] = RevisionMetrics(2500.0, 0.0, 5.0, 100)
    env.reconcile(rid)
    assert env.reconcile(rid).after is RolloutStatus.ROLLED_BACK
    assert "p95 latency" in (env.rollout(rid).rollout.status_reason or "")
    assert env.serving.split(REF) == {1: 100}


def test_no_traffic_means_no_promotion(env: Env) -> None:
    env.metrics.by_revision[(REF, 2)] = RevisionMetrics(None, None, 0.0, 0)
    rid = env.start()
    env.reconcile(rid)
    result = env.run_step(rid)
    assert result.after is RolloutStatus.ROLLED_BACK
    assert "need 20" in (env.rollout(rid).rollout.status_reason or "")
    assert env.status(2) is ModelStatus.REJECTED and env.status(1) is ModelStatus.CHAMPION


def test_a_canary_that_fails_to_load_is_rolled_back(env: Env) -> None:
    env.serving.auto_ready = False
    rid = env.start()
    env.reconcile(rid)
    env.serving.mark_failed(REF, "model could not be loaded")
    assert env.reconcile(rid).after is RolloutStatus.ROLLED_BACK
    assert "model could not be loaded" in (env.rollout(rid).rollout.status_reason or "")
    assert env.serving.split(REF) == {1: 100}


def test_a_canary_that_never_becomes_ready_times_out(env: Env) -> None:
    env.serving.auto_ready = False
    rid = env.start(gate=RolloutGate(ready_timeout_seconds=30, step_seconds=60, min_requests=20))
    env.reconcile(rid)
    assert env.reconcile(rid).after is RolloutStatus.PROGRESSING  # still loading
    env.clock.advance(31)
    assert env.reconcile(rid).after is RolloutStatus.ROLLED_BACK
    assert "did not become ready" in (env.rollout(rid).rollout.status_reason or "")


# -- abort and interference -----------------------------------------------------


def test_abort_before_traffic_moves_just_ends_it(env: Env) -> None:
    rid = env.start()
    calls = env.serving.deploy_calls
    assert env.rollouts.abort(rid).rollout.status is RolloutStatus.ROLLED_BACK
    assert env.serving.deploy_calls == calls
    assert env.rollouts.abort(rid).rollout.status is RolloutStatus.ROLLED_BACK  # idempotent
    assert env.status(2) is ModelStatus.CANDIDATE  # nothing proved it bad; it can be retried


def test_abort_mid_rollout_returns_all_traffic_to_stable(env: Env) -> None:
    rid = env.start()
    env.reconcile(rid)
    env.run_step(rid)
    assert env.serving.split(REF) == {2: 25, 1: 75}
    assert env.rollouts.abort(rid).rollout.abort_requested
    assert env.reconcile(rid).after is RolloutStatus.ROLLED_BACK
    assert env.serving.split(REF) == {1: 100}
    assert env.rollout(rid).rollout.status_reason == "aborted by user"


def test_the_deployment_reconciler_leaves_a_canary_alone(env: Env) -> None:
    rid = env.start()
    env.reconcile(rid)
    calls = env.serving.deploy_calls
    for _ in range(3):
        env.deploy_rec.reconcile(env.deployment_id())
    assert env.serving.deploy_calls == calls and env.serving.split(REF) == {2: 10, 1: 90}
    env.rollouts.abort(rid)
    env.reconcile(rid)
    assert env.deploy_rec.reconcile(env.deployment_id()).after is DeploymentStatus.READY


def test_losing_the_serving_resource_mid_rollout_restores_stable_not_the_canary(env: Env) -> None:
    rid = env.start()
    env.reconcile(rid)
    env.run_step(rid)
    env.serving.delete(REF)  # both revisions vanish with the resource
    assert env.reconcile(rid).after is RolloutStatus.ROLLED_BACK
    # Recreating only the canary would give an unproven model 100% of the traffic.
    assert env.serving.specs[REF].revision == 1 and env.serving.split(REF) == {1: 100}
    assert "disappeared" in (env.rollout(rid).rollout.status_reason or "")


def test_a_resource_changed_to_another_revision_mid_rollout_is_rolled_back(env: Env) -> None:
    rid = env.start()
    env.reconcile(rid)
    env.serving.specs[REF] = replace(env.serving.specs[REF], revision=7)
    assert env.reconcile(rid).after is RolloutStatus.ROLLED_BACK
    assert env.serving.specs[REF].revision == 1


# -- the champion moves in the same transaction as the evidence ------------------


class _FailOn:
    def __init__(self, inner: UnitOfWork, action: str) -> None:
        self._inner, self._action = inner, action

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def audit(self) -> Any:
        inner, action = self._inner.audit, self._action

        class Guard:
            def record(self, event: Any) -> None:
                if event.action == action:
                    raise RuntimeError("audit store down")
                inner.record(event)

        return Guard()

    def __enter__(self) -> Self:
        self._inner.__enter__()
        return self

    def __exit__(
        self, et: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._inner.__exit__(et, e, tb)

    def commit(self) -> None:
        self._inner.commit()


def test_success_is_atomic_with_the_promotion(env: Env) -> None:
    rid = env.start()
    env.reconcile(rid)
    for _ in range(3):
        env.run_step(rid)
    broken = RolloutReconciler(
        lambda: cast(UnitOfWork, _FailOn(env.factory(), "rollout.succeeded")),
        env.serving,
        env.metrics,
        env.clock,
    )
    broken.reconcile(rid)  # observing
    env.clock.advance(61)
    with pytest.raises(RuntimeError):
        broken.reconcile(rid)  # final step passes, but the success transaction fails
    assert env.rollout(rid).rollout.status is RolloutStatus.PROGRESSING
    assert (env.status(1), env.status(2)) == (ModelStatus.CHAMPION, ModelStatus.CANDIDATE)
    assert env.deployment().active_revision == 1

    assert env.reconcile(rid).after is RolloutStatus.SUCCEEDED  # healthy retry completes it
    assert env.status(2) is ModelStatus.CHAMPION


# -- rolling a deployment back ---------------------------------------------------


def _succeeded(env: Env) -> UUID:
    rid = env.start()
    env.reconcile(rid)
    for _ in range(4):
        env.run_step(rid)
    assert env.rollout(rid).rollout.status is RolloutStatus.SUCCEEDED
    return rid


def test_rollback_returns_to_the_previous_revision_and_champion(env: Env) -> None:
    _succeeded(env)
    revisions_before = env.deployments.get("credit-risk", "credit-risk-prod").revisions
    view = env.deployments.rollback("credit-risk", "credit-risk-prod")
    assert view.deployment.desired_revision == 1
    assert view.deployment.status is DeploymentStatus.DEPLOYING
    assert (env.status(1), env.status(2)) == (ModelStatus.CHAMPION, ModelStatus.ARCHIVED)
    assert "deployment.rolled_back" in env.audit(project_id=env.project_id)

    assert env.deploy_rec.reconcile(env.deployment_id()).after is DeploymentStatus.READY
    assert env.serving.specs[REF].revision == 1
    assert env.deployment().active_revision == 1
    assert env.deployments.get("credit-risk", "credit-risk-prod").revisions == revisions_before


def test_rollback_is_idempotent_and_guarded(env: Env) -> None:
    with pytest.raises(Conflict, match="no earlier revision"):
        env.deployments.rollback("credit-risk", "credit-risk-prod")  # only revision 1 exists
    _succeeded(env)
    env.deployments.rollback("credit-risk", "credit-risk-prod")
    env.deploy_rec.reconcile(env.deployment_id())
    again = env.deployments.rollback("credit-risk", "credit-risk-prod", 1)
    assert again.deployment.desired_revision == 1  # already there


def test_rollback_is_refused_during_a_rollout(env: Env) -> None:
    env.start()
    with pytest.raises(Conflict, match="rollout is in progress"):
        env.deployments.rollback("credit-risk", "credit-risk-prod")


# -- API ------------------------------------------------------------------------


def test_api_flow(uow_factory: Factory, env: Env) -> None:
    client = TestClient(
        create_app(uow_factory, env.clock, experiments=env.experiments, serving=env.serving)
    )
    base = "/projects/credit-risk/deployments/credit-risk-prod"
    assert (
        client.post(
            f"{base}/rollouts", json={"model": "scorer", "version": 2, "steps": [50]}
        ).status_code
        == 422
    )
    started = client.post(
        f"{base}/rollouts",
        json={
            "model": "scorer",
            "version": 2,
            "steps": [20, 100],
            "gate": {"step_seconds": 0, "min_requests": 1},
        },
    )
    assert started.status_code == 202
    body = started.json()
    assert body["status"] == "PENDING" and body["traffic"] == {"1": 100, "2": 0}
    assert not {"external_ref", "model_uri"} & set(body)

    assert (
        client.post(f"{base}/rollouts", json={"model": "scorer", "version": 3}).status_code == 409
    )
    env.reconcile(UUID(body["id"]))
    live = client.get(f"/rollouts/{body['id']}").json()
    assert live["status"] == "PROGRESSING" and live["canary_percent"] == 20
    assert live["traffic"] == {"1": 80, "2": 20}

    aborted = client.post(f"/rollouts/{body['id']}/abort")
    assert aborted.status_code == 202 and aborted.json()["abort_requested"] is True
    env.reconcile(UUID(body["id"]))
    assert client.get(f"/rollouts/{body['id']}").json()["status"] == "ROLLED_BACK"
    assert len(client.get(f"{base}/rollouts").json()["items"]) == 1
    assert client.post(f"{base}/rollback").status_code == 409  # no earlier revision to return to
