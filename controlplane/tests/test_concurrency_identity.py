"""Regression cases for identity reuse and transaction interleavings; no cluster required."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from typing import Any

import pytest

from controlplane.adapters.identity import OidcProvider
from controlplane.api.session import (
    SESSION_PURPOSE,
    Signer,
    principal_from_session,
    principal_to_session,
)
from controlplane.application.members import MembershipService
from controlplane.application.ports import UnitOfWork
from controlplane.application.providers import ExternalState
from controlplane.domain.access import ProjectRole
from controlplane.domain.entities import EndpointLimits, Threshold
from controlplane.domain.errors import AlreadyExists, Conflict
from controlplane.domain.states import EndpointKind, EndpointProtocol, ModelStatus, RolloutStatus
from controlplane.persistence.sql import SqlUnitOfWork
from controlplane.tests.conftest import FakeClock
from controlplane.tests.test_pipelines import Env as PipeEnv
from controlplane.tests.test_pipelines import step
from controlplane.tests.test_rollouts import Env as RollEnv
from controlplane.tests.test_runs import JOB
from controlplane.tests.test_runs import Env as RunEnv


def test_oidc_identity_survives_rename_and_refuses_username_reuse() -> None:
    provider = OidcProvider("https://idp.example", audience="mlp", platform_admins=["user:alice"])
    alice = provider.principal({"sub": "A", "preferred_username": "alice"})
    renamed = provider.principal({"sub": "A", "preferred_username": "new-name"})
    impostor = provider.principal({"sub": "B", "preferred_username": "alice"})
    foreign = OidcProvider("https://other.example", audience="mlp").principal(
        {"sub": "A", "preferred_username": "alice"}
    )
    assert alice.user_subject == renamed.user_subject
    assert alice.user_subject != impostor.user_subject != foreign.user_subject
    assert not alice.platform_admin and not impostor.platform_admin
    admin_provider = OidcProvider(
        "https://idp.example", audience="mlp", platform_admins=[alice.user_subject]
    )
    assert admin_provider.principal({"sub": "A"}).platform_admin
    assert not admin_provider.principal({"sub": "B", "preferred_username": "alice"}).platform_admin
    assert principal_from_session(principal_to_session(alice)) == alice
    signer = Signer("x" * 40)
    old = signer.dumps(principal_to_session(alice), purpose="session-v2", ttl_seconds=60)
    assert signer.loads(old, purpose=SESSION_PURPOSE) is None


@pytest.mark.parametrize("race", [False, True])
@pytest.mark.parametrize("different", ["commit", "definition", "timeout"])
def test_pipeline_fingerprint_includes_execution_inputs(
    uow_factory: Callable[[], UnitOfWork],
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    race: bool,
    different: str,
) -> None:
    env = PipeEnv(uow_factory, clock)
    env.pipeline("flow", step("a"))
    env.pipeline("flow", step("a"), step("b", "a"))
    env.runs.create("credit-risk", "flow", version=1, commit_sha="a", idempotency_key="k")
    kwargs: dict[str, Any] = dict(
        version=1, commit_sha="a", timeout_seconds=3600, idempotency_key="k"
    )
    kwargs[
        {"commit": "commit_sha", "definition": "version", "timeout": "timeout_seconds"}[different]
    ] = {"commit": "b", "definition": 2, "timeout": 42}[different]
    if race:

        def collide(*args: Any, **kw: Any) -> Any:
            raise AlreadyExists("pipeline run", "k")

        monkeypatch.setattr(env.runs, "_create", collide)
    with pytest.raises(Conflict):
        env.runs.create("credit-risk", "flow", **kwargs)


@pytest.mark.parametrize("race", [False, True])
def test_retry_key_cannot_replay_another_parent(
    uow_factory: Callable[[], UnitOfWork],
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    race: bool,
) -> None:
    env = RunEnv(uow_factory, clock)
    env.jobs.create("credit-risk", JOB)
    originals = []
    for _ in range(2):
        run, _ = env.runs.create("credit-risk", JOB.name)
        env.reconciler.reconcile(run.id)
        ref = env.runs.get(run.id).external_ref
        assert ref is not None
        env.workflow.set_state(ref, ExternalState.FAILED)
        env.reconciler.reconcile(run.id)
        originals.append(run)
    first, _ = env.runs.retry(originals[0].id, idempotency_key="k")
    replay, created = env.runs.retry(originals[0].id, idempotency_key="k")
    assert replay.id == first.id and not created
    if race:

        def collide(*args: Any, **kw: Any) -> Any:
            raise AlreadyExists("run", "k")

        monkeypatch.setattr(env.runs, "_create", collide)
    with pytest.raises(Conflict):
        env.runs.retry(originals[1].id, idempotency_key="k")


def test_candidate_reservation_is_enforced_in_service_and_repository(
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    env = RollEnv(uow_factory)
    first = env.start()
    env.deployments.create("credit-risk", "second")
    env.deployments.deploy("credit-risk", "second", "scorer", 1)
    second_id = env.deployments.get("credit-risk", "second").deployment.id
    env.deploy_rec.reconcile(second_id)
    with pytest.raises(Conflict, match="model version"):
        env.rollouts.start("credit-risk", "second", "scorer", 2)
    rollout = env.rollout(first).rollout
    with pytest.raises(AlreadyExists), uow_factory() as uow:
        uow.rollouts.add(replace(rollout, id=second_id, deployment_id=second_id))
        uow.commit()


def test_rejected_candidate_restores_stable_and_finishes(
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    env = RollEnv(uow_factory)
    rid = env.start(steps=(100,))
    env.reconcile(rid)
    with uow_factory() as uow:
        candidate = uow.model_versions.get(env.rollout(rid).rollout.model_version_id)
        assert candidate is not None
        uow.model_versions.update(
            candidate.transition_to(ModelStatus.REJECTED, env.clock()),
            expected_status=ModelStatus.CANDIDATE,
        )
        uow.commit()
    result = env.reconcile(rid)
    assert result.after == RolloutStatus.ROLLED_BACK
    assert env.rollout(rid).traffic == {1: 100, 2: 0}
    assert env.serving.specs["mlp-credit-risk/credit-risk-prod"].revision == 1
    assert env.reconcile(rid).after == RolloutStatus.ROLLED_BACK


@pytest.mark.parametrize("method", ["deploy", "ensure_revision"])
def test_version_rejected_during_lookup_is_not_deployed(
    uow_factory: Callable[[], UnitOfWork], monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    env = RollEnv(uow_factory)
    original = env.deployments._artifact

    def reject(project: Any, model: Any, version: Any) -> Any:
        uri = original(project, model, version)
        with uow_factory() as uow:
            uow.model_versions.update(
                version.transition_to(ModelStatus.REJECTED, env.clock()),
                expected_status=version.status,
            )
            uow.commit()
        return uri

    monkeypatch.setattr(env.deployments, "_artifact", reject)
    with pytest.raises(Conflict, match="no longer deployable"):
        getattr(env.deployments, method)("credit-risk", "credit-risk-prod", "scorer", 2)
    assert len(env.deployments.get("credit-risk", "credit-risk-prod").revisions) == 1


def test_revision_rechecks_after_interleaved_lookup(
    uow_factory: Callable[[], UnitOfWork], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = RollEnv(uow_factory)
    original = env.deployments._artifact
    interleaved: list[bool] = []

    def create(project: Any, model: Any, version: Any) -> Any:
        uri = original(project, model, version)
        if not interleaved:
            interleaved.append(True)
            env.deployments.ensure_revision("credit-risk", "credit-risk-prod", "scorer", 2)
        return uri

    monkeypatch.setattr(env.deployments, "_artifact", create)
    revision = env.deployments.ensure_revision("credit-risk", "credit-risk-prod", "scorer", 2)
    with uow_factory() as uow:
        assert (
            len(
                [
                    r
                    for r in uow.revisions.list(env.deployment_id())
                    if r.model_version_id == revision.model_version_id
                ]
            )
            == 1
        )


def test_stale_field_owners_preserve_other_changes(uow_factory: Callable[[], UnitOfWork]) -> None:
    env = RollEnv(uow_factory)
    with uow_factory() as uow:
        model = uow.models.get(env.model.id)
        endpoint = uow.endpoints.get_by_deployment(env.deployment_id())
    assert model is not None and endpoint is not None
    # Stale aggregate supplied deliberately in both write orders.
    with uow_factory() as uow:
        uow.models.update_alias_drift(model.with_alias_drift("drift"))
        uow.endpoints.update_access(
            endpoint.exposed(endpoint.exposure, EndpointLimits(units_per_minute=77), env.clock())
        )
        uow.commit()
    with uow_factory() as uow:
        uow.models.update_thresholds(replace(model, thresholds={"auc": Threshold(min=0.95)}))
        uow.endpoints.update_lifecycle(endpoint, expected_status=endpoint.status)
        uow.commit()
    with uow_factory() as uow:
        current_model = uow.models.get(model.id)
        assert current_model is not None and current_model.alias_drift == "drift"
        current_endpoint = uow.endpoints.get_by_deployment(env.deployment_id())
        assert current_endpoint is not None and current_endpoint.limits.units_per_minute == 77
        uow.models.update_alias_drift(model.with_alias_drift(None))
        uow.commit()
    with uow_factory() as uow:
        current_model = uow.models.get(model.id)
        assert current_model is not None and current_model.thresholds == {
            "auc": Threshold(min=0.95)
        }

        uow.endpoints.update_lifecycle(
            replace(endpoint, kind=EndpointKind.LLM, protocol=EndpointProtocol.OPENAI),
            expected_status=endpoint.status,
        )
        uow.commit()
    with uow_factory() as uow:
        uow.endpoints.update_access(
            endpoint.exposed(endpoint.exposure, EndpointLimits(units_per_minute=88), env.clock())
        )
        uow.commit()
    with uow_factory() as uow:
        current_endpoint = uow.endpoints.get_by_deployment(env.deployment_id())
        assert current_endpoint is not None
        assert (
            current_endpoint.kind == EndpointKind.LLM
            and current_endpoint.protocol == EndpointProtocol.OPENAI
        )
        assert current_endpoint.limits.units_per_minute == 88


@pytest.mark.parametrize("demote", [False, True])
def test_sql_concurrent_last_admin_changes_are_serialized(
    uow_factory: Callable[[], UnitOfWork], demote: bool
) -> None:
    if not isinstance(uow_factory(), SqlUnitOfWork):
        pytest.skip("requires real row locks; memory adapter is not a concurrency oracle")
    RollEnv(uow_factory)
    members = MembershipService(uow_factory)
    for name in ("alice", "bob"):
        members.set_role("credit-risk", f"user:{name}", ProjectRole.ADMIN)
    start = Barrier(2)

    def leave(name: str) -> str:
        start.wait(timeout=5)
        try:
            if demote:
                members.set_role("credit-risk", f"user:{name}", ProjectRole.VIEWER)
            else:
                members.remove("credit-risk", f"user:{name}")
            return "changed"
        except Conflict:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(leave, ("alice", "bob")))
    assert sorted(results) == ["blocked", "changed"]
    assert sum(m.role == ProjectRole.ADMIN for m in members.list("credit-risk")) == 1


def test_sql_version_lock_blocks_state_change_through_commit(
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    if not isinstance(uow_factory(), SqlUnitOfWork):
        pytest.skip("requires PostgreSQL row locks")
    from threading import Event

    env = RollEnv(uow_factory)
    with uow_factory() as uow:
        versions = uow.model_versions.list(env.model.id)
        candidate = next(v for v in versions if v.version == 2)
    started = Event()
    finished = Event()

    def reject() -> None:
        with uow_factory() as writer:
            started.set()
            writer.model_versions.update(
                candidate.transition_to(ModelStatus.REJECTED, env.clock()),
                expected_status=ModelStatus.CANDIDATE,
            )
            writer.commit()
        finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with uow_factory() as reader:
            assert reader.model_versions.lock(candidate.id) is not None
            task = pool.submit(reject)
            assert started.wait(timeout=5)
            assert not finished.wait(timeout=0.2)
            reader.commit()
        task.result(timeout=5)
    assert finished.is_set() and env.status(2) == ModelStatus.REJECTED


def test_rejection_during_final_promotion_rolls_back(
    uow_factory: Callable[[], UnitOfWork], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = RollEnv(uow_factory)
    rid = env.start(steps=(100,))
    env.reconcile(rid)
    env.reconcile(rid)
    env.clock.advance(61)
    original = env.rollout_rec._succeed

    def reject_then_succeed(ctx: Any, reason: str) -> Any:
        with uow_factory() as uow:
            candidate = uow.model_versions.get(ctx.rollout.model_version_id)
            assert candidate is not None
            uow.model_versions.update(
                candidate.transition_to(ModelStatus.REJECTED, env.clock()),
                expected_status=ModelStatus.CANDIDATE,
            )
            uow.commit()
        return original(ctx, reason)

    monkeypatch.setattr(env.rollout_rec, "_succeed", reject_then_succeed)
    assert env.reconcile(rid).after == RolloutStatus.ROLLED_BACK
    assert env.rollout(rid).traffic == {1: 100, 2: 0}
