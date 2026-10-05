"""Manual promotion and canary reservation share a version lock, without a cluster."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
from typing import Any
from uuid import UUID

import pytest

from controlplane.application.models import PromotionService
from controlplane.application.ports import UnitOfWork
from controlplane.domain.entities import ModelVersion
from controlplane.domain.errors import Conflict
from controlplane.domain.states import ModelStatus, RolloutStatus
from controlplane.persistence.sql import SqlModelVersions, SqlUnitOfWork
from controlplane.tests.test_rollouts import Env

Factory = Callable[[], UnitOfWork]


@pytest.mark.parametrize("progressing", [False, True])
def test_active_rollout_owns_promotion(uow_factory: Factory, progressing: bool) -> None:
    env = Env(uow_factory)
    rid = env.start(steps=(100,))
    if progressing:
        env.reconcile(rid)
    candidate = env.rollout(rid).rollout.model_version_id
    with pytest.raises(Conflict, match="promotion is decided by the rollout"):
        PromotionService(uow_factory, env.clock).promote(candidate)
    assert env.status(1) == ModelStatus.CHAMPION
    assert env.status(2) == ModelStatus.CANDIDATE
    env.rollouts.abort(rid)
    env.reconcile(rid)
    assert env.rollout(rid).rollout.status == RolloutStatus.ROLLED_BACK
    assert env.status(1) == ModelStatus.CHAMPION
    if progressing:
        assert env.status(2) == ModelStatus.REJECTED
    else:
        # A canary aborted before traffic does not reject its candidate.
        PromotionService(uow_factory, env.clock).promote(candidate)
        assert env.status(2) == ModelStatus.CHAMPION


def test_successful_rollout_can_promote_and_manual_replay_is_allowed(uow_factory: Factory) -> None:
    env = Env(uow_factory)
    rid = env.start(steps=(100,))
    env.reconcile(rid)
    env.run_step(rid)
    assert env.rollout(rid).rollout.status == RolloutStatus.SUCCEEDED
    assert env.status(2) == ModelStatus.CHAMPION
    candidate = env.rollout(rid).rollout.model_version_id
    PromotionService(uow_factory, env.clock).promote(candidate)
    assert env.status(2) == ModelStatus.CHAMPION


@pytest.mark.parametrize("first", ["rollout", "promotion"])
def test_sql_manual_promotion_and_rollout_start_serialize(
    uow_factory: Factory, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    if not isinstance(uow_factory(), SqlUnitOfWork):
        pytest.skip("requires PostgreSQL row locks")
    env = Env(uow_factory)
    # Avoid pausing the earlier artifact/revision transaction; pause the reservation itself.
    revision = env.deployments.ensure_revision("credit-risk", "credit-risk-prod", "scorer", 2)
    promotion = PromotionService(uow_factory, env.clock)
    locked, release, second_started, second_finished = Event(), Event(), Event(), Event()
    owner: list[int] = []
    original = SqlModelVersions.lock

    def pause(self: SqlModelVersions, version_id: UUID) -> ModelVersion | None:
        version = original(self, version_id)
        if (
            version_id == revision.model_version_id
            and get_ident() == owner[0]
            and not locked.is_set()
        ):
            locked.set()
            assert release.wait(timeout=5)
        return version

    monkeypatch.setattr(SqlModelVersions, "lock", pause)

    def act(action: str) -> Any:
        if action == "promotion":
            return promotion.promote(revision.model_version_id)
        return env.rollouts.start("credit-risk", "credit-risk-prod", "scorer", 2)

    def winner() -> Any:
        owner.append(get_ident())
        return act(first)

    def loser() -> str:
        second_started.set()
        try:
            act("promotion" if first == "rollout" else "rollout")
            return "completed"
        except Conflict:
            return "conflict"
        finally:
            second_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(winner)
        assert locked.wait(timeout=5)
        b = pool.submit(loser)
        try:
            assert second_started.wait(timeout=5)
            assert not second_finished.wait(timeout=0.2)
        finally:
            release.set()
        a.result(timeout=5)
        assert b.result(timeout=5) == ("conflict" if first == "rollout" else "completed")
    assert env.status(2) == (ModelStatus.CANDIDATE if first == "rollout" else ModelStatus.CHAMPION)
    with uow_factory() as uow:
        assert uow.rollouts.get_active_by_version(revision.model_version_id) is not None
