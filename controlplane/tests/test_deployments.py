"""M18 skeleton tests: deployments, revisions, endpoints, readiness, drift recovery."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from controlplane.adapters.fakes import (
    FakeClusterProvider,
    FakeExperimentProvider,
    FakeServingProvider,
)
from controlplane.adapters.serving.kserve import build_inference_service
from controlplane.api.app import create_app
from controlplane.application.deployments import DeploymentService, serving_ref
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import CreateProject, ProjectService
from controlplane.application.providers import RegisteredVersion, ServingSpec
from controlplane.domain.entities import (
    Deployment,
    DeploymentRevision,
    Model,
    ModelVersion,
    Project,
)
from controlplane.domain.errors import AlreadyExists, Conflict, NotFound
from controlplane.domain.states import DeploymentStatus, EndpointStatus, ModelStatus
from controlplane.reconciliation.deployments import DeploymentReconciler
from controlplane.reconciliation.projects import ProjectReconciler

Factory = Callable[[], UnitOfWork]
REGISTRY = "credit-risk-scorer"


class Env:
    def __init__(self, factory: Factory, clock: Callable[[], Any]) -> None:
        self.factory = factory
        self.clock = clock
        self.experiments = FakeExperimentProvider()
        self.serving = FakeServingProvider()
        self.service = DeploymentService(factory, clock, self.experiments, self.serving)
        self.reconciler = DeploymentReconciler(factory, self.serving, clock)
        project, _ = ProjectService(factory, clock).create(CreateProject(name="credit-risk"))
        ProjectReconciler(factory, FakeClusterProvider(), clock).reconcile(project.id)
        self.project_id = project.id
        with factory() as uow:
            self.model = Model.create(
                project_id=project.id, name="scorer", thresholds={}, now=clock()
            )
            uow.models.add(self.model)
            uow.commit()
        self.version(ModelStatus.CANDIDATE)  # v1
        self.version(ModelStatus.CANDIDATE)  # v2
        self.version(ModelStatus.REGISTERED)  # v3
        self.version(ModelStatus.REJECTED)  # v4
        self.service.create("credit-risk", "credit-risk-prod")

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

    def deploy(self, version: int) -> tuple[Any, bool]:
        return self.service.deploy("credit-risk", "credit-risk-prod", "scorer", version)

    def view(self) -> Any:
        return self.service.get("credit-risk", "credit-risk-prod")

    def id(self) -> UUID:
        return self.view().deployment.id  # type: ignore[no-any-return]

    def ref(self) -> str:
        return "mlp-credit-risk/credit-risk-prod"

    def audit(self) -> list[str]:
        with self.factory() as uow:
            return [e.action for e in uow.audit.list(project_id=self.project_id)]

    def assert_endpoint_consistent(self) -> None:
        """The invariant: a READY deployment never has a non-READY endpoint."""
        v = self.view()
        if v.deployment.status is DeploymentStatus.READY:
            assert v.endpoint.status is EndpointStatus.READY


@pytest.fixture
def env(uow_factory: Factory, clock: Callable[[], Any]) -> Env:
    return Env(uow_factory, clock)


def test_provider_failure_does_not_starve_another_deployment(env: Env) -> None:
    from unittest.mock import Mock, patch

    from controlplane.reconciliation.batch import ReconcileBackoff

    now = Mock(return_value=0)
    env.reconciler._retry = ReconcileBackoff(now)

    env.deploy(1)
    env.service.create("credit-risk", "other-prod")
    env.service.deploy("credit-risk", "other-prod", "scorer", 1)
    original = env.serving.get_status

    def status(ref: str) -> Any:
        if ref == env.ref():
            raise ConnectionError("one backend is unreachable")
        return original(ref)

    with patch.object(env.serving, "get_status", side_effect=status):
        env.reconciler.reconcile_all()
    assert env.view().deployment.status is not DeploymentStatus.READY
    assert env.service.get("credit-risk", "other-prod").deployment.status is DeploymentStatus.READY
    assert all(r.deployment_id != env.id() for r in env.reconciler.reconcile_all())
    now.return_value = 5
    env.reconciler.reconcile_all()
    assert env.view().deployment.status is DeploymentStatus.READY


# -- creation and the approval rule -------------------------------------------


def test_deployment_and_endpoint_exist_before_anything_is_served(env: Env) -> None:
    view = env.view()
    assert view.deployment.status is DeploymentStatus.PENDING
    assert (
        view.endpoint.status is EndpointStatus.PENDING and view.endpoint.name == "credit-risk-prod"
    )
    assert view.deployment.desired_revision is None
    again, created = env.service.create("credit-risk", "credit-risk-prod")
    assert not created and again.deployment.id == view.deployment.id


def test_candidate_deploys_and_the_database_object_comes_first(env: Env) -> None:
    view, created = env.deploy(1)
    assert created and view.deployment.status is DeploymentStatus.DEPLOYING
    assert view.deployment.desired_revision == 1 and view.endpoint.status is EndpointStatus.PENDING
    assert env.serving.deploy_calls == 0  # nothing touched the serving system yet
    assert [r.revision.revision for r in view.revisions] == [1]


@pytest.mark.parametrize("version", [3, 4])
def test_unapproved_versions_cannot_be_deployed(env: Env, version: int) -> None:
    with pytest.raises(Conflict, match="only"):
        env.deploy(version)
    assert env.view().revisions == []


def test_unknown_targets_are_not_found(env: Env) -> None:
    with pytest.raises(NotFound):
        env.deploy(99)
    with pytest.raises(NotFound):
        env.service.deploy("credit-risk", "credit-risk-prod", "ghost", 1)
    with pytest.raises(NotFound):
        env.service.deploy("credit-risk", "ghost", "scorer", 1)


# -- readiness is observed, never assumed -------------------------------------


def test_ready_requires_the_model_to_actually_be_serving(env: Env) -> None:
    env.serving.auto_ready = False
    env.deploy(1)
    result = env.reconciler.reconcile(env.id())
    assert result.applied and result.after is DeploymentStatus.DEPLOYING
    spec = env.serving.specs[env.ref()]
    assert (spec.namespace, spec.name, spec.revision) == ("mlp-credit-risk", "credit-risk-prod", 1)
    assert spec.model_uri == f"s3://models/{REGISTRY}/1"
    assert env.view().endpoint.status is EndpointStatus.PENDING  # not READY yet
    env.assert_endpoint_consistent()

    env.reconciler.reconcile(env.id())  # still loading: nothing to record, nothing re-deployed
    assert env.serving.deploy_calls == 1

    env.serving.mark_ready(env.ref())
    assert env.reconciler.reconcile(env.id()).after is DeploymentStatus.READY
    view = env.view()
    assert view.endpoint.status is EndpointStatus.READY and view.endpoint.url
    assert view.deployment.active_revision == 1
    env.assert_endpoint_consistent()
    assert {"deployment.ready", "endpoint.ready"} <= set(env.audit())


def test_converged_deployment_causes_no_writes(env: Env) -> None:
    env.deploy(1)
    env.reconciler.reconcile(env.id())
    events, calls = len(env.audit()), env.serving.deploy_calls
    for _ in range(10):
        assert env.reconciler.reconcile(env.id()).after is DeploymentStatus.READY
    assert (len(env.audit()), env.serving.deploy_calls) == (events, calls)


def test_prediction_goes_through_the_platform_only_when_ready(env: Env) -> None:
    env.serving.auto_ready = False
    env.deploy(1)
    env.reconciler.reconcile(env.id())
    with pytest.raises(Conflict, match="not READY"):
        env.service.predict("credit-risk", "credit-risk-prod", {"instances": [[1, 2, 3]]})
    env.serving.mark_ready(env.ref())
    env.reconciler.reconcile(env.id())
    out = env.service.predict("credit-risk", "credit-risk-prod", {"instances": [[1, 2, 3]]})
    assert out == {"predictions": [0.0]}
    assert env.serving.requests == [(env.ref(), {"instances": [[1, 2, 3]]})]


# -- revisions ----------------------------------------------------------------


def test_new_model_is_a_new_revision_and_history_is_untouched(env: Env) -> None:
    env.deploy(1)
    env.reconciler.reconcile(env.id())
    first: DeploymentRevision = env.view().revisions[0].revision

    view, created = env.deploy(2)
    assert created and view.deployment.desired_revision == 2
    assert view.deployment.status is DeploymentStatus.DEPLOYING
    assert view.endpoint.status is EndpointStatus.READY  # old revision keeps serving meanwhile
    assert view.deployment.active_revision == 1

    assert env.reconciler.reconcile(env.id()).after is DeploymentStatus.READY
    final = env.view()
    assert final.deployment.active_revision == 2
    assert [r.revision.revision for r in final.revisions] == [1, 2]
    assert final.revisions[0].revision == first  # revision 1 is exactly as it was
    assert env.serving.specs[env.ref()].model_uri == f"s3://models/{REGISTRY}/2"


def test_redeploying_the_latest_version_creates_no_revision(env: Env) -> None:
    env.deploy(1)
    again, created = env.deploy(1)
    assert not created and len(again.revisions) == 1


def test_revisions_are_append_only(env: Env) -> None:
    env.deploy(1)
    with env.factory() as uow:
        existing = uow.revisions.list(env.id())[0]
        duplicate = DeploymentRevision(
            deployment_id=existing.deployment_id,
            revision=existing.revision,
            model_version_id=existing.model_version_id,
            model_uri="s3://elsewhere",
            created_at=existing.created_at,
        )
        with pytest.raises(AlreadyExists):
            uow.revisions.add(duplicate)
    assert not hasattr(uow.revisions, "update")


# -- drift and failure --------------------------------------------------------


def test_a_deleted_serving_resource_is_recreated_from_the_revision(env: Env) -> None:
    env.deploy(1)
    env.reconciler.reconcile(env.id())
    env.serving.delete(env.ref())  # someone deletes the InferenceService by hand
    result = env.reconciler.reconcile(env.id())
    assert result.applied and result.after is DeploymentStatus.READY
    assert env.ref() in env.serving.specs
    actions = env.audit()
    assert {"deployment.drift_detected", "deployment.redeploying"} <= set(actions)
    env.assert_endpoint_consistent()


def test_while_recreating_the_endpoint_is_unavailable_not_ready(env: Env) -> None:
    env.deploy(1)
    env.reconciler.reconcile(env.id())
    env.serving.auto_ready = False
    env.serving.delete(env.ref())
    assert env.reconciler.reconcile(env.id()).after is DeploymentStatus.DEPLOYING
    assert env.view().endpoint.status is EndpointStatus.UNAVAILABLE
    with pytest.raises(Conflict):
        env.service.predict("credit-risk", "credit-risk-prod", {"instances": []})
    env.serving.mark_ready(env.ref())
    assert env.reconciler.reconcile(env.id()).after is DeploymentStatus.READY
    assert env.view().endpoint.status is EndpointStatus.READY


def test_serving_failure_fails_the_deployment_and_a_new_revision_retries(env: Env) -> None:
    env.serving.auto_ready = False
    env.deploy(1)
    env.reconciler.reconcile(env.id())
    env.serving.mark_failed(env.ref(), "model could not be loaded")
    assert env.reconciler.reconcile(env.id()).after is DeploymentStatus.FAILED
    failed = env.view()
    assert failed.deployment.status_reason == "model could not be loaded"
    assert failed.endpoint.status is EndpointStatus.PENDING  # never READY, so never UNAVAILABLE

    calls = env.serving.deploy_calls
    assert env.reconciler.reconcile(env.id()).after is DeploymentStatus.FAILED  # no auto-retry
    assert env.serving.deploy_calls == calls

    env.serving.auto_ready = True
    view, _ = env.deploy(2)
    assert view.deployment.status is DeploymentStatus.DEPLOYING
    assert env.reconciler.reconcile(env.id()).after is DeploymentStatus.READY


def test_deployments_need_a_ready_project(uow_factory: Factory, clock: Callable[[], Any]) -> None:
    ProjectService(uow_factory, clock).create(CreateProject(name="cold"))
    with pytest.raises(Conflict, match="READY"):
        DeploymentService(uow_factory, clock).create("cold", "dep-one")


# -- pure ---------------------------------------------------------------------


def test_kserve_manifest_loads_the_revisions_artifact() -> None:
    manifest = build_inference_service(
        ServingSpec(
            name="credit-risk-prod",
            namespace="mlp-credit-risk",
            model_uri="s3://models/scorer/1",
            revision=3,
            labels={"mlp.io/revision": "3"},
        )
    )
    assert manifest["kind"] == "InferenceService"
    assert manifest["metadata"]["annotations"]["mlp.io/revision"] == "3"
    model = manifest["spec"]["predictor"]["model"]
    assert model["storageUri"] == "s3://models/scorer/1"
    assert model["modelFormat"] == {"name": "mlflow"}


def test_serving_ref_is_deterministic() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    project = Project.create(name="credit-risk", display_name=None, description="", now=now)
    deployment = Deployment.create(project_id=project.id, name="credit-risk-prod", now=now)
    assert serving_ref(project, deployment) == "mlp-credit-risk/credit-risk-prod"
    assert serving_ref(project, deployment) == serving_ref(project, deployment)


# -- API ----------------------------------------------------------------------


def test_api_flow(uow_factory: Factory, clock: Callable[[], Any], env: Env) -> None:
    client = TestClient(
        create_app(uow_factory, clock, experiments=env.experiments, serving=env.serving)
    )
    assert (
        client.post(
            "/projects/credit-risk/deployments", json={"name": "credit-risk-prod"}
        ).status_code
        == 200
    )
    created = client.post("/projects/credit-risk/deployments", json={"name": "second-dep"})
    assert created.status_code == 201 and created.json()["status"] == "PENDING"

    rejected = client.post(
        "/projects/credit-risk/deployments/credit-risk-prod/revisions",
        json={"model": "scorer", "version": 4},
    )
    assert rejected.status_code == 409
    accepted = client.post(
        "/projects/credit-risk/deployments/credit-risk-prod/revisions",
        json={"model": "scorer", "version": 1},
    )
    assert accepted.status_code == 202 and accepted.json()["desired_revision"] == 1
    assert "model_uri" not in accepted.json()["revisions"][0]
    assert (
        client.post(
            "/projects/credit-risk/deployments/credit-risk-prod/revisions",
            json={"model": "scorer", "version": 1},
        ).status_code
        == 200
    )

    assert (
        client.post(
            "/projects/credit-risk/endpoints/credit-risk-prod/predict", json={"instances": [[1]]}
        ).status_code
        == 409
    )  # endpoint not READY yet

    env.reconciler.reconcile(env.id())
    assert (
        client.get("/projects/credit-risk/endpoints/credit-risk-prod").json()["status"] == "READY"
    )
    predicted = client.post(
        "/projects/credit-risk/endpoints/credit-risk-prod/predict", json={"instances": [[1], [2]]}
    )
    assert predicted.status_code == 200 and predicted.json() == {"predictions": [0.0, 0.0]}
    assert (
        client.get("/projects/credit-risk/deployments/credit-risk-prod").json()["status"] == "READY"
    )
    assert len(client.get("/projects/credit-risk/deployments").json()["items"]) == 2


def test_deletion_closes_endpoint_then_confirms_backend_removal(env: Env) -> None:
    from unittest.mock import patch

    from controlplane.application.providers import ServingState, ServingStatus

    env.deploy(1)
    env.reconciler.reconcile(env.id())
    deleted = env.service.request_delete("credit-risk", "credit-risk-prod")
    assert deleted.deployment.status is DeploymentStatus.DELETING
    assert deleted.endpoint.status is EndpointStatus.UNAVAILABLE
    with pytest.raises(Conflict):
        env.deploy(2)
    # Asynchronous deletion must retain reservations until the backend is absent.
    with (
        patch.object(env.serving, "delete"),
        patch.object(env.serving, "get_status", return_value=ServingStatus(ServingState.PENDING)),
    ):
        assert env.reconciler.reconcile(env.id()).after is DeploymentStatus.DELETING
    assert env.view().deployment.desired_revision == 1
    assert env.reconciler.reconcile(env.id()).after is DeploymentStatus.DELETED
    assert env.view().deployment.active_revision is None
    assert env.view().deployment.desired_revision is None
    assert env.service.list("credit-risk") == []
    assert (
        env.service.request_delete("credit-risk", "credit-risk-prod").deployment.status
        is DeploymentStatus.DELETED
    )
    assert env.reconciler.reconcile_all() == []
    with pytest.raises(Conflict):
        env.service.create("credit-risk", "credit-risk-prod")


def test_pending_deployment_can_be_deleted_without_revision(env: Env) -> None:
    env.service.request_delete("credit-risk", "credit-risk-prod")
    assert env.reconciler.reconcile_all()[0].after is DeploymentStatus.DELETED


def test_same_revision_configuration_drift_is_repaired_once(env: Env) -> None:
    from dataclasses import replace

    env.deploy(1)
    env.reconciler.reconcile(env.id())
    original = env.serving.specs[env.ref()]
    env.serving.specs[env.ref()] = replace(original, model_uri="s3://wrong-artifact")
    result = env.reconciler.reconcile(env.id())
    assert result.applied and env.serving.specs[env.ref()] == original
    assert not env.reconciler.reconcile(env.id()).applied
