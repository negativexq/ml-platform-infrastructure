"""Functions: your own container behind an endpoint, scaled by Knative, called through the
gateway like any model."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from controlplane.adapters.fakes import FakeClusterProvider, FakeServingProvider
from controlplane.adapters.gateway import HttpUpstream, ServingUpstream, TokenBucketLimiter
from controlplane.adapters.serving.kserve import build_inference_service
from controlplane.application.api_access import ApiAccessService
from controlplane.application.deployments import DeploymentService
from controlplane.application.gateway import GatewayService, UpstreamCall
from controlplane.application.models import ModelService, PromotionService
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import CreateProject, ProjectService
from controlplane.application.providers import ServingSpec
from controlplane.domain.entities import EndpointLimits, FunctionServing, Threshold
from controlplane.domain.errors import Conflict, InvalidArgument
from controlplane.domain.states import (
    EndpointKind,
    EndpointProtocol,
    Exposure,
    ModelKind,
    ModelStatus,
    ServingRuntime,
)
from controlplane.gateway import create_gateway
from controlplane.reconciliation.deployments import DeploymentReconciler
from controlplane.reconciliation.projects import ProjectReconciler
from controlplane.tests.test_gateway import Monotonic, Recorder

Factory = Callable[[], UnitOfWork]
IMAGE = "ghcr.io/acme/ticket-router@sha256:" + "a" * 64
SETTINGS = FunctionServing(min_scale=0, max_scale=5, concurrency=20, env={"LOG_LEVEL": "info"})


class Env:
    def __init__(self, factory: Factory, clock: Callable[[], Any]) -> None:
        self.factory, self.clock = factory, clock
        project, _ = ProjectService(factory, clock).create(CreateProject(name="support"))
        ProjectReconciler(factory, FakeClusterProvider(), clock).reconcile(project.id)
        self.serving = FakeServingProvider()
        self.models = ModelService(factory, clock)
        self.deployments = DeploymentService(factory, clock, None, self.serving)
        self.models.create(
            "support", "ticket-router", {}, kind=ModelKind.FUNCTION, function=SETTINGS
        )
        self.deployments.create("support", "ticket-router")

    def serve(self) -> None:
        self.models.register_image("support", "ticket-router", IMAGE)
        self.deployments.deploy("support", "ticket-router", "ticket-router", 1)
        DeploymentReconciler(self.factory, self.serving, self.clock).reconcile_all()


@pytest.fixture
def env(uow_factory: Factory, clock: Callable[[], Any]) -> Env:
    return Env(uow_factory, clock)


def test_a_function_version_is_an_image_and_a_candidate_at_once(env: Env) -> None:
    version, created = env.models.register_image("support", "ticket-router", IMAGE)
    assert created and version.status is ModelStatus.CANDIDATE and version.source_uri == IMAGE
    again, created = env.models.register_image("support", "ticket-router", IMAGE)
    assert not created and again.id == version.id
    for bad in (
        "ticket-router",
        "ghcr.io/acme/ticket-router",
        "ghcr.io/acme/ticket-router:latest",
        "ghcr.io/acme/ticket-router:1.4.2",
        "ghcr.io/acme/ticket-router@sha256:abc",
        "ghcr.io/acme/ticket-router@sha256:" + "A" * 64,
    ):
        with pytest.raises(InvalidArgument):
            env.models.register_image("support", "ticket-router", bad)
    env.models.create("support", "scorer", {})
    with pytest.raises(Conflict, match="not a function"):
        env.models.register_image("support", "scorer", IMAGE)
    with pytest.raises(InvalidArgument, match="no evaluation thresholds"):
        env.models.create("support", "x-fn", {"auc": Threshold(min=0.9)}, kind=ModelKind.FUNCTION)
    with pytest.raises(InvalidArgument):
        FunctionServing(min_scale=4, max_scale=2)
    with pytest.raises(InvalidArgument):
        FunctionServing(env={"bad-name": "1"})


def test_deploying_a_function_serves_its_image_with_its_scaling(env: Env) -> None:
    env.serve()
    view = env.deployments.get("support", "ticket-router")
    (revision,) = view.revisions
    assert revision.revision.runtime is ServingRuntime.CONTAINER
    assert revision.revision.model_uri == IMAGE and revision.revision.function == SETTINGS
    assert revision.revision.gpus == 0
    endpoint = view.endpoint
    assert (endpoint.kind, endpoint.protocol) == (EndpointKind.FUNCTION, EndpointProtocol.HTTP)
    assert endpoint.limits.max_body_kb == 1024 and endpoint.limits.timeout_seconds == 60
    answer = env.deployments.predict("support", "ticket-router", {"ticket": "my card was declined"})
    assert answer == {
        "function": "ticket-router",
        "received": {"ticket": "my card was declined"},
        "ok": True,
    }


def test_kserve_runs_the_function_as_its_own_container() -> None:
    body = build_inference_service(
        ServingSpec(
            name="ticket-router",
            namespace="mlp-support",
            model_uri=IMAGE,
            revision=1,
            runtime="container",
            function=SETTINGS.to_json(),
        )
    )
    predictor = body["spec"]["predictor"]
    assert "model" not in predictor
    assert (predictor["minReplicas"], predictor["maxReplicas"]) == (0, 5)
    assert predictor["containerConcurrency"] == 20
    (container,) = predictor["containers"]
    assert container["name"] == "kserve-container" and container["image"] == IMAGE
    assert container["ports"] == [{"containerPort": 8080, "protocol": "TCP"}]
    assert container["env"] == [{"name": "LOG_LEVEL", "value": "info"}]
    assert container["readinessProbe"]["tcpSocket"] == {"port": 8080}


def test_function_resources_and_probe_survive_serialization() -> None:
    settings = FunctionServing(
        requests={"cpu": "250m", "memory": "256Mi"},
        limits={"cpu": "2", "memory": "1Gi"},
        readiness_path="/health/ready",
        readiness_timeout_seconds=4,
    )
    restored = FunctionServing.from_json(settings.to_json())
    assert restored == settings
    body = build_inference_service(
        ServingSpec(
            name="fn",
            namespace="mlp-a",
            model_uri=IMAGE,
            revision=1,
            runtime="container",
            function=restored.to_json(),
        )
    )
    container = body["spec"]["predictor"]["containers"][0]
    assert container["resources"]["requests"] == dict(settings.requests)
    assert container["readinessProbe"]["httpGet"] == {"path": "/health/ready", "port": 8080}
    assert container["readinessProbe"]["timeoutSeconds"] == 4
    assert FunctionServing.from_json({}).requests == {"cpu": "100m", "memory": "128Mi"}
    with pytest.raises(InvalidArgument, match="request exceeds"):
        FunctionServing(requests={"cpu": "2", "memory": "128Mi"})
    with pytest.raises(InvalidArgument):
        FunctionServing(readiness_path="relative")


def test_function_rollback_restores_the_original_digest(env: Env) -> None:
    env.serve()
    promotions = PromotionService(env.factory, env.clock)
    promotions.promote(env.models.get("support", "ticket-router").versions[0].id)
    next_image = "ghcr.io/acme/ticket-router@sha256:" + "b" * 64
    candidate, _ = env.models.register_image("support", "ticket-router", next_image)
    promotions.promote(candidate.id)
    env.deployments.deploy("support", "ticket-router", "ticket-router", 2)
    reconciler = DeploymentReconciler(env.factory, env.serving, env.clock)
    reconciler.reconcile_all()
    env.deployments.rollback("support", "ticket-router", 1)
    reconciler.reconcile_all()
    view = env.deployments.get("support", "ticket-router")
    assert view.deployment.active_revision == 1
    original = next(r.revision for r in view.revisions if r.revision.revision == 1)
    assert original.model_uri == IMAGE
    assert (
        build_inference_service(
            ServingSpec(
                name="ticket-router",
                namespace="mlp-support",
                model_uri=original.model_uri,
                revision=1,
                runtime="container",
            )
        )["spec"]["predictor"]["containers"][0]["image"]
        == IMAGE
    )


def test_a_function_is_called_through_the_gateway_at_invoke(env: Env) -> None:
    env.serve()
    access = ApiAccessService(env.factory, env.clock)
    access.expose("support", "ticket-router", Exposure.PUBLIC, EndpointLimits())
    token = access.create_key("support", name="helpdesk", endpoints=["ticket-router"])[1]
    usage = Recorder()
    gateway = GatewayService(
        env.factory,
        ServingUpstream(env.serving),
        TokenBucketLimiter(Monotonic()),
        recorders=[usage],
        clock=env.clock,
        monotonic=Monotonic(),
    )
    client = TestClient(create_gateway(gateway))
    auth = {"authorization": f"Bearer {token}"}
    reply = client.post("/v1/support/ticket-router/invoke", json={"ticket": "hi"}, headers=auth)
    assert reply.status_code == 200 and reply.json()["received"] == {"ticket": "hi"}
    wrong = client.post("/v1/support/ticket-router/predict", json={}, headers=auth)
    assert wrong.status_code == 404 and "/invoke" in wrong.json()["error"]["message"]
    (call, _) = usage.calls
    assert (call.unit, call.units, call.status) == ("requests", 1, 200)


@pytest.mark.anyio
async def test_http_upstream_posts_to_the_container_root() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"ok": True})

    upstream = HttpUpstream(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await upstream.call(
        UpstreamCall(
            ref="mlp-support/ticket-router",
            url="http://ticket-router.mlp-support.svc",
            protocol=EndpointProtocol.HTTP,
            body=b"{}",
            timeout_seconds=5,
            request_id="r",
        )
    )
    assert seen == ["/"]


def test_function_api_flow(uow_factory: Factory, clock: Any) -> None:
    from controlplane.api.app import create_app

    project, _ = ProjectService(uow_factory, clock).create(CreateProject(name="support"))
    ProjectReconciler(uow_factory, FakeClusterProvider(), clock).reconcile(project.id)
    serving = FakeServingProvider()
    client = TestClient(create_app(uow_factory, clock, serving=serving))
    base = "/projects/support"
    created = client.post(
        f"{base}/models",
        json={
            "name": "ticket-router",
            "kind": "function",
            "function": {"min_scale": 1, "max_scale": 4, "env": {"MODE": "fast"}},
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["function"]["max_scale"] == 4 and created.json()["kind"] == "function"
    assert client.post(f"{base}/models", json={"name": "x-fn", "function": {}}).status_code == 422
    version = client.post(f"{base}/models/ticket-router/images", json={"image": IMAGE})
    assert version.status_code == 201 and version.json()["status"] == "CANDIDATE"
    assert (
        client.post(f"{base}/models/ticket-router/images", json={"image": IMAGE}).status_code == 200
    )
    client.post(f"{base}/deployments", json={"name": "ticket-router"})
    deployed = client.post(
        f"{base}/deployments/ticket-router/revisions", json={"model": "ticket-router", "version": 1}
    )
    assert deployed.status_code == 202, deployed.text
    revision = deployed.json()["revisions"][0]
    assert (revision["runtime"], revision["min_scale"], revision["max_scale"]) == (
        "container",
        1,
        4,
    )
    DeploymentReconciler(uow_factory, serving, clock).reconcile_all()
    called = client.post(f"{base}/endpoints/ticket-router/predict", json={"ticket": "hello"})
    assert called.status_code == 200 and called.json()["received"] == {"ticket": "hello"}
    access = client.get(f"{base}/endpoints/ticket-router/access").json()
    assert (access["kind"], access["protocol"], access["operation"]) == (
        "function",
        "http",
        "invoke",
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
