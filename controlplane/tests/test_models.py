"""M17 skeleton tests: models, discovery, evaluation, promotion, alias sync."""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Any, Self, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from controlplane.adapters.fakes import FakeExperimentProvider
from controlplane.api.app import create_app
from controlplane.application.models import EvaluationService, ModelService, PromotionService
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import CreateProject, ProjectService
from controlplane.application.providers import ExperimentRun, RegisteredVersion
from controlplane.application.workflow_compiler import TAG_PIPELINE_RUN_ID
from controlplane.domain.entities import Threshold, run_checks
from controlplane.domain.errors import Conflict, InvalidArgument
from controlplane.domain.states import ModelKind, ModelStatus
from controlplane.reconciliation.model_aliases import ModelAliasReconciler

Factory = Callable[[], UnitOfWork]
GOOD = {"auc": 0.95, "f1": 0.9}
BAD = {"auc": 0.8, "f1": 0.9}
THRESHOLDS = {"auc": Threshold(min=0.9), "f1": Threshold(min=0.85)}
REGISTRY = "credit-risk-scorer"


# -- pure rules ----------------------------------------------------------------


def test_threshold_rules() -> None:
    assert Threshold(min=0.9).check(0.9) and not Threshold(min=0.9).check(0.89)
    assert Threshold(max=0.1).check(0.1) and not Threshold(max=0.1).check(0.11)
    assert Threshold(min=0.1, max=0.2).check(0.15)
    assert not Threshold(min=0.0).check(None)  # never reported cannot pass
    for bad in ({}, {"min": 2, "max": 1}):
        with pytest.raises(InvalidArgument):
            Threshold(**bad)


def test_run_checks_covers_every_threshold_including_missing_metrics() -> None:
    checks = run_checks({"auc": 0.95}, THRESHOLDS)
    assert [(c.metric, c.value, c.passed) for c in checks] == [
        ("auc", 0.95, True),
        ("f1", None, False),
    ]


# -- fixtures ------------------------------------------------------------------


class FlakyExperiments(FakeExperimentProvider):
    """The fake registry, with switches to simulate outages."""

    def __init__(self) -> None:
        super().__init__()
        self.tracker_down = False
        self.alias_writes_fail = False

    def get_run(self, ref: str) -> ExperimentRun:
        if self.tracker_down:
            raise ConnectionError("tracker down")
        # runs are stored as "<experiment>/<run>" by the fixture
        return super().get_run(f"exp/{ref}" if ref.startswith("run-") else ref)

    def set_model_alias(self, model: str, alias: str, version_ref: str) -> None:
        if self.alias_writes_fail:
            raise ConnectionError("registry down")
        super().set_model_alias(model, alias, version_ref)


class Env:
    def __init__(self, factory: Factory, clock: Callable[[], Any]) -> None:
        self.factory = factory
        self.experiments = FlakyExperiments()
        self.models = ModelService(factory, clock, self.experiments)
        self.evals = EvaluationService(factory, self.experiments, clock)
        self.promotions = PromotionService(factory, clock)
        self.aliases = ModelAliasReconciler(factory, self.experiments, clock)
        self.pipeline_run = uuid4()
        project, _ = ProjectService(factory, clock).create(CreateProject(name="credit-risk"))
        self.project_id = project.id
        self.models.create("credit-risk", "scorer", THRESHOLDS)

    def register(self, ref: str, metrics: dict[str, float], *, tracked: bool = True) -> None:
        """Pretend training finished and registered a version in the registry."""
        run_ref = f"run-{ref}" if tracked else None
        if tracked:
            self.experiments.runs[f"exp/{run_ref}"] = ExperimentRun(
                ref=f"run-{ref}",
                metrics=metrics,
                tags={TAG_PIPELINE_RUN_ID: str(self.pipeline_run)},
            )
        self.experiments.registered.setdefault(REGISTRY, []).append(RegisteredVersion(ref, run_ref))

    def discover(self) -> list[UUID]:
        return [v.id for v in self.models.discover("credit-risk", "scorer").created]

    def candidate(self, ref: str, metrics: dict[str, float] | None = None) -> UUID:
        self.register(ref, metrics or GOOD)
        version_id = self.discover()[-1]
        self.evals.evaluate(version_id)
        return version_id

    def status(self, version_id: UUID) -> ModelStatus:
        return self.models.get_version(version_id).version.status

    def audit(self, **kw: Any) -> list[str]:
        with self.factory() as uow:
            return [e.action for e in uow.audit.list(**kw)]


@pytest.fixture
def env(uow_factory: Factory, clock: Callable[[], Any]) -> Env:
    return Env(uow_factory, clock)


# -- models --------------------------------------------------------------------


def test_model_create_is_idempotent_and_thresholds_change_explicitly(env: Env) -> None:
    view, created = env.models.create("credit-risk", "scorer", THRESHOLDS)
    assert not created and view.registry_name == REGISTRY
    with pytest.raises(Conflict, match="thresholds"):
        env.models.create("credit-risk", "scorer", {"auc": Threshold(min=0.5)})
    changed = env.models.set_thresholds("credit-risk", "scorer", {"auc": Threshold(min=0.5)})
    assert set(changed.model.thresholds) == {"auc"}
    assert "model.thresholds_changed" in env.audit(project_id=env.project_id)


# -- discovery -----------------------------------------------------------------


def test_discovery_registers_versions_once_with_lineage(env: Env) -> None:
    env.register("1", GOOD)
    first = env.models.discover("credit-risk", "scorer")
    assert [v.version for v in first.created] == [1]
    assert first.created[0].source_pipeline_run_id == env.pipeline_run
    assert first.created[0].status is ModelStatus.REGISTERED

    again = env.models.discover("credit-risk", "scorer")
    assert (len(again.created), again.existing) == (0, 1)  # no duplicate version

    env.register("2", GOOD)
    third = env.models.discover("credit-risk", "scorer")
    assert [v.version for v in third.created] == [2] and third.existing == 1
    assert len(env.models.get("credit-risk", "scorer").versions) == 2


# -- evaluation ----------------------------------------------------------------


def test_passing_evaluation_makes_a_candidate_and_records_the_checks(env: Env) -> None:
    env.register("1", GOOD)
    version_id = env.discover()[0]
    view = env.evals.evaluate(version_id)
    assert view.version.status is ModelStatus.CANDIDATE
    [evaluation] = view.evaluations
    assert evaluation.status.value == "PASSED"
    assert {c.metric: c.passed for c in evaluation.checks} == {"auc": True, "f1": True}
    assert evaluation.metrics == GOOD


def test_failing_evaluation_rejects_and_a_rejected_version_cannot_be_promoted(env: Env) -> None:
    env.register("1", BAD)
    version_id = env.discover()[0]
    view = env.evals.evaluate(version_id)
    assert view.version.status is ModelStatus.REJECTED
    assert [c.metric for c in view.evaluations[0].checks if not c.passed] == ["auc"]
    with pytest.raises(Conflict, match="rejected"):
        env.promotions.promote(version_id)
    with pytest.raises(Conflict):
        env.evals.evaluate(version_id)  # terminal: no second chance by re-evaluating


def test_a_version_without_a_tracking_run_is_rejected_not_waved_through(env: Env) -> None:
    env.register("1", GOOD, tracked=False)
    version_id = env.discover()[0]
    assert env.evals.evaluate(version_id).version.status is ModelStatus.REJECTED


def test_evaluation_needs_thresholds(env: Env) -> None:
    env.models.set_thresholds("credit-risk", "scorer", {})
    env.register("1", GOOD)
    with pytest.raises(Conflict, match="no thresholds"):
        env.evals.evaluate(env.discover()[0])


def test_a_metrics_outage_leaves_the_version_evaluating_and_retryable(env: Env) -> None:
    env.register("1", GOOD)
    version_id = env.discover()[0]
    env.experiments.tracker_down = True
    with pytest.raises(ConnectionError):
        env.evals.evaluate(version_id)
    assert env.status(version_id) is ModelStatus.EVALUATING  # not REJECTED

    env.experiments.tracker_down = False
    assert env.evals.evaluate(version_id).version.status is ModelStatus.CANDIDATE
    assert len(env.models.get_version(version_id).evaluations) == 1  # resumed, not duplicated


# -- promotion -----------------------------------------------------------------


def test_promotion_archives_the_old_champion_and_keeps_history(env: Env) -> None:
    v1, v2 = env.candidate("1"), env.candidate("2")
    first = env.promotions.promote(v1)
    assert first.version.status is ModelStatus.CHAMPION
    assert first.promotions[0].previous_champion_id is None

    second = env.promotions.promote(v2)
    assert second.version.status is ModelStatus.CHAMPION
    assert second.promotions[0].previous_champion_id == v1
    assert env.status(v1) is ModelStatus.ARCHIVED  # history kept, no longer champion
    assert [p.status.value for p in env.models.get_version(v1).promotions] == ["APPLIED"]
    assert env.models.get("credit-risk", "scorer").champion is not None
    assert env.audit(entity_id=v2).count("model_version.promoted") == 1


def test_promotion_is_idempotent_and_only_candidates_qualify(env: Env) -> None:
    v1 = env.candidate("1")
    env.promotions.promote(v1)
    again = env.promotions.promote(v1)
    assert again.version.status is ModelStatus.CHAMPION and len(again.promotions) == 1
    env.register("2", GOOD)
    registered = env.discover()[0]
    with pytest.raises(Conflict, match="CANDIDATE"):
        env.promotions.promote(registered)


def test_a_model_can_never_have_two_champions(env: Env) -> None:
    v1, v2 = env.candidate("1"), env.candidate("2")
    env.promotions.promote(v1)
    with env.factory() as uow:
        second = uow.model_versions.get(v2)
        assert second is not None
        with pytest.raises(Conflict, match="champion"):
            uow.model_versions.update(
                second.transition_to(ModelStatus.CHAMPION, second.updated_at),
                expected_status=ModelStatus.CANDIDATE,
            )


class _FailingAudit:
    def __init__(self, inner: UnitOfWork) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def audit(self) -> Any:
        class Boom:
            def record(self, event: Any) -> None:
                raise RuntimeError("audit down")

        return Boom()

    def __enter__(self) -> Self:
        self._inner.__enter__()
        return self

    def __exit__(
        self, et: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._inner.__exit__(et, e, tb)

    def commit(self) -> None:
        self._inner.commit()


def test_promotion_is_atomic_with_its_audit_record(env: Env, clock: Callable[[], Any]) -> None:
    v1, v2 = env.candidate("1"), env.candidate("2")
    env.promotions.promote(v1)
    broken = PromotionService(lambda: cast(UnitOfWork, _FailingAudit(env.factory())), clock)
    with pytest.raises(RuntimeError):
        broken.promote(v2)
    assert env.status(v1) is ModelStatus.CHAMPION  # old champion untouched
    assert env.status(v2) is ModelStatus.CANDIDATE
    assert len(env.models.get_version(v2).promotions) == 0


# -- registry alias sync -------------------------------------------------------


@pytest.mark.parametrize("kind", [ModelKind.FUNCTION, ModelKind.LLM])
def test_non_registry_models_never_call_mlflow_aliases(env: Env, kind: ModelKind) -> None:
    from unittest.mock import Mock

    view, _ = env.models.create("credit-risk", "non-registry", {}, kind=kind)
    if kind is ModelKind.FUNCTION:
        env.models.register_image(
            "credit-risk", "non-registry", "ghcr.io/acme/image@sha256:" + "a" * 64
        )
    else:
        env.models.register_from_hub("credit-risk", "non-registry", "hf://org/model@abc123", {})
    experiments = Mock(spec=FakeExperimentProvider)
    aliases = ModelAliasReconciler(env.factory, experiments)
    assert aliases.reconcile(view.model.id).synced == ()
    assert all(result.model_id != view.model.id for result in aliases.reconcile_all())
    assert experiments.mock_calls == []
    assert env.models.get("credit-risk", "non-registry").model.alias_drift is None


def test_aliases_follow_platform_state(env: Env) -> None:
    v1, v2 = env.candidate("1"), env.candidate("2")
    model_id = env.models.get("credit-risk", "scorer").model.id
    env.aliases.reconcile(model_id)
    assert env.experiments.get_model_alias(REGISTRY, "candidate") == "2"  # newest candidate
    assert env.experiments.get_model_alias(REGISTRY, "champion") is None

    env.promotions.promote(v1)
    result = env.aliases.reconcile(model_id)
    assert env.experiments.get_model_alias(REGISTRY, "champion") == "1"
    assert result.drift_detected is False  # first sync is not drift
    assert env.aliases.reconcile(model_id).synced == ()  # converged: nothing to do
    assert v2 and env.models.get("credit-risk", "scorer").model.alias_drift is None


def test_a_manually_moved_alias_is_detected_recorded_and_repaired(env: Env) -> None:
    v1, _ = env.candidate("1"), env.candidate("2")
    env.promotions.promote(v1)
    model_id = env.models.get("credit-risk", "scorer").model.id
    env.aliases.reconcile(model_id)

    env.experiments.set_model_alias(REGISTRY, "champion", "2")  # someone edits MLflow by hand
    result = env.aliases.reconcile(model_id)
    assert result.drift_detected
    assert env.experiments.get_model_alias(REGISTRY, "champion") == "1"  # platform wins
    actions = env.audit(entity_id=model_id)
    assert "model.alias_drift_detected" in actions and "model.alias_synced" in actions


def test_a_failed_repair_leaves_the_drift_flag_set(env: Env) -> None:
    v1 = env.candidate("1")
    env.promotions.promote(v1)
    model_id = env.models.get("credit-risk", "scorer").model.id
    env.aliases.reconcile(model_id)
    env.experiments.set_model_alias(REGISTRY, "champion", "9")

    env.experiments.alias_writes_fail = True
    env.aliases.reconcile_all()
    drift = env.models.get("credit-risk", "scorer").model.alias_drift
    assert drift is not None and "registry down" in drift


# -- API -----------------------------------------------------------------------


def test_api_flow(uow_factory: Factory, clock: Callable[[], Any], env: Env) -> None:
    client = TestClient(create_app(uow_factory, clock, experiments=env.experiments))
    body = {"name": "scorer", "thresholds": {"auc": {"min": 0.9}, "f1": {"min": 0.85}}}
    assert (
        client.post("/projects/credit-risk/models", json=body).status_code == 200
    )  # fixture made it
    assert (
        client.post(
            "/projects/credit-risk/models", json={**body, "thresholds": {"auc": {"min": 0.1}}}
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/projects/credit-risk/models", json={"name": "bad", "thresholds": {"auc": {}}}
        ).status_code
        == 422
    )

    env.register("1", GOOD)
    env.register("2", BAD)
    found = client.post("/projects/credit-risk/models/scorer/discover").json()
    assert [v["version"] for v in found["created"]] == [1, 2]
    assert client.post("/projects/credit-risk/models/scorer/discover").json()["already_known"] == 2
    good, bad = (v["id"] for v in found["created"])
    assert not any("external" in v for v in found["created"])

    assert client.post(f"/model-versions/{good}/evaluate").json()["status"] == "CANDIDATE"
    rejected = client.post(f"/model-versions/{bad}/evaluate").json()
    assert rejected["status"] == "REJECTED"
    assert client.post(f"/model-versions/{bad}/promote").status_code == 409

    promoted = client.post(f"/model-versions/{good}/promote").json()
    assert promoted["status"] == "CHAMPION" and promoted["promotions"][0]["status"] == "APPLIED"
    model = client.get("/projects/credit-risk/models/scorer").json()
    assert model["champion"]["id"] == good and model["registry_name"] == REGISTRY
    assert len(client.get("/projects/credit-risk/models/scorer/versions").json()["items"]) == 2
    assert client.get(f"/model-versions/{uuid4()}").status_code == 404
