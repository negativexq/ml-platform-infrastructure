"""LLMs: hub versions judged on their own results, GPU quota, serving, chat and tokens."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from controlplane.adapters.fakes import (
    FakeClusterProvider,
    FakeExperimentProvider,
    FakeServingProvider,
)
from controlplane.adapters.gateway import HttpUpstream, ServingUpstream, TokenBucketLimiter
from controlplane.adapters.serving.kserve import build_inference_service
from controlplane.application.api_access import ApiAccessService
from controlplane.application.deployments import DeploymentService
from controlplane.application.gateway import CACHE_SECONDS, CallRecord, GatewayService
from controlplane.application.llm import TokenMeter, prepare_chat
from controlplane.application.models import EvaluationService, ModelService, PromotionService
from controlplane.application.namespaces import namespace_spec
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import CreateProject, ProjectService
from controlplane.application.providers import ServingSpec
from controlplane.application.rollouts import RolloutService
from controlplane.domain.entities import EndpointLimits, LlmServing, ModelVersion, Threshold
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
QWEN = "hf://Qwen/Qwen2.5-7B-Instruct@a09a354"
QWEN_NEXT = "hf://Qwen/Qwen2.5-7B-Instruct@bb46c15"
CHAT = {"messages": [{"role": "user", "content": "Why was my loan declined?"}]}


class Env:
    def __init__(self, factory: Factory, clock: Callable[[], Any]) -> None:
        self.factory, self.clock = factory, clock
        self.projects = ProjectService(factory, clock)
        project, _ = self.projects.create(CreateProject(name="support"))
        self.cluster = FakeClusterProvider()
        ProjectReconciler(factory, self.cluster, clock).reconcile(project.id)
        self.serving = FakeServingProvider()
        self.models = ModelService(factory, clock, FakeExperimentProvider())
        self.evaluations = EvaluationService(factory, None, clock)
        self.promotions = PromotionService(factory, clock)
        self.deployments = DeploymentService(factory, clock, None, self.serving)
        self.reconciler = DeploymentReconciler(factory, self.serving, clock)
        self.models.create(
            "support",
            "assistant",
            {"helpfulness": Threshold(min=0.7), "toxicity": Threshold(max=0.01)},
            kind=ModelKind.LLM,
            serving=LlmServing(gpus=1, context_length=8192),
        )
        self.deployments.create("support", "assistant-prod")

    def version(self, source: str = QWEN, **metrics: float) -> Any:
        version, _ = self.models.register_from_hub(
            "support", "assistant", source, metrics or {"helpfulness": 0.82, "toxicity": 0.002}
        )
        return self.evaluations.evaluate(version.id).version

    def serve(self, gpus: int = 1) -> None:
        self.projects.set_gpu_quota("support", gpus)
        self.deployments.deploy("support", "assistant-prod", "assistant", 1)
        self.reconciler.reconcile_all()


@pytest.fixture
def env(uow_factory: Factory, clock: Callable[[], Any]) -> Env:
    return Env(uow_factory, clock)


def test_hub_versions_are_judged_on_the_results_they_came_with(env: Env) -> None:
    good = env.version()
    assert good.status is ModelStatus.CANDIDATE and good.source_uri == QWEN
    again, created = env.models.register_from_hub("support", "assistant", QWEN, {})
    assert not created and again.id == good.id  # the same source is the same version
    bad = env.version(QWEN_NEXT, helpfulness=0.9, toxicity=0.05)
    assert bad.status is ModelStatus.REJECTED
    with pytest.raises(InvalidArgument, match="hf://"):
        env.models.register_from_hub("support", "assistant", "s3://bucket/llm", {})
    env.models.create("support", "scorer", {})
    with pytest.raises(Conflict, match="registry"):
        env.models.register_from_hub("support", "scorer", QWEN, {})
    with pytest.raises(InvalidArgument):
        LlmServing(gpus=0)


def test_an_llm_needs_gpu_quota_and_says_how_much(env: Env) -> None:
    env.version()
    with pytest.raises(Conflict, match="needs 1 GPU and the project's quota is 0"):
        env.deployments.deploy("support", "assistant-prod", "assistant", 1)
    with pytest.raises(InvalidArgument):
        env.projects.set_gpu_quota("support", 1000)
    env.serve(gpus=1)
    view = env.deployments.get("support", "assistant-prod")
    (revision,) = view.revisions
    assert (revision.revision.runtime, revision.revision.gpus) == (ServingRuntime.HUGGINGFACE, 1)
    assert revision.revision.model_uri == QWEN and revision.revision.context_length == 8192
    endpoint = view.endpoint
    assert (endpoint.kind, endpoint.protocol) == (EndpointKind.LLM, EndpointProtocol.OPENAI)
    assert endpoint.limits.units_per_minute == 20_000 and endpoint.limits.timeout_seconds == 120
    with pytest.raises(Conflict, match="1 GPUs are in use"):
        env.projects.set_gpu_quota("support", 0)
    with env.factory() as uow:
        project = uow.projects.get_by_name("support")
    assert project is not None
    quota = namespace_spec(project).quota
    assert quota["requests.nvidia.com/gpu"] == "1" and quota["limits.memory"] == "40Gi"


def test_a_canary_holds_gpus_next_to_the_stable_revision(env: Env) -> None:
    env.version()
    env.serve(gpus=1)
    with env.factory() as uow:
        v1 = next(v for v in uow.model_versions.list(env_model(uow).id) if v.version == 1)
    env.promotions.promote(v1.id)
    env.version(QWEN_NEXT)
    rollouts = RolloutService(env.factory, env.deployments, env.clock)
    with pytest.raises(Conflict, match="needs 2 GPUs"):
        rollouts.start("support", "assistant-prod", "assistant", 2)
    env.projects.set_gpu_quota("support", 2)
    assert rollouts.start("support", "assistant-prod", "assistant", 2).rollout.to_revision == 2


def env_model(uow: UnitOfWork) -> Any:
    project = uow.projects.get_by_name("support")
    assert project is not None
    model = uow.models.get_by_name(project.id, "assistant")
    assert model is not None
    return model


def test_kinds_do_not_mix_in_one_deployment(env: Env, uow_factory: Factory) -> None:
    env.version()
    env.serve()
    with pytest.raises(Conflict, match="is an LLM"):  # predict is for classic models
        env.deployments.predict("support", "assistant-prod", {"instances": [[1]]})
    scorer, _ = env.models.create("support", "scorer", {})
    with uow_factory() as uow:
        now = env.clock()
        uow.model_versions.add(
            ModelVersion(
                model_id=scorer.model.id,
                version=1,
                status=ModelStatus.CANDIDATE,
                external_ref="1",
                created_at=now,
                updated_at=now,
            )
        )
        uow.commit()
    with pytest.raises(Conflict, match="serves huggingface models"):
        env.deployments.deploy("support", "assistant-prod", "scorer", 1)
    answer = env.deployments.chat("support", "assistant-prod", CHAT)
    assert answer["choices"][0]["message"]["role"] == "assistant"
    assert answer["usage"]["total_tokens"] > 0


def test_kserve_runs_an_llm_on_gpus_with_its_context() -> None:
    body = build_inference_service(
        ServingSpec(
            name="assistant-prod",
            namespace="mlp-support",
            model_uri=QWEN,
            revision=1,
            runtime="huggingface",
            gpus=2,
            context_length=8192,
        )
    )
    model = body["spec"]["predictor"]["model"]
    assert model["modelFormat"] == {"name": "huggingface"} and model["storageUri"] == QWEN
    assert model["args"] == [
        "--model_name=assistant-prod",
        "--max_model_len=8192",
        "--tensor_parallel_size=2",
    ]
    assert model["resources"]["limits"]["nvidia.com/gpu"] == "2"
    assert model["env"][0]["valueFrom"]["secretKeyRef"]["optional"] is True
    classic = build_inference_service(
        ServingSpec(name="x", namespace="mlp-x", model_uri="s3://m", revision=1)
    )
    assert classic["spec"]["predictor"]["model"]["modelFormat"] == {"name": "mlflow"}


# -- through the gateway ---------------------------------------------------------------------


class Gateway:
    def __init__(self, env: Env) -> None:
        env.version()
        env.serve()
        access = ApiAccessService(env.factory, env.clock)
        access.expose(
            "support",
            "assistant-prod",
            Exposure.PUBLIC,
            EndpointLimits(units_per_minute=200, max_body_kb=64, timeout_seconds=60),
        )
        self.token = access.create_key("support", name="helpdesk", endpoints=["assistant-prod"])[1]
        self.time = Monotonic()
        self.usage = Recorder()
        service = GatewayService(
            env.factory,
            ServingUpstream(env.serving),
            TokenBucketLimiter(self.time),
            recorders=[self.usage],
            clock=env.clock,
            monotonic=self.time,
        )
        self.client = TestClient(create_gateway(service))
        self.serving = env.serving

    def chat(self, body: dict[str, Any] | None = None) -> httpx.Response:
        response: httpx.Response = self.client.post(
            "/v1/support/assistant-prod/chat/completions",
            json=CHAT if body is None else body,
            headers={"authorization": f"Bearer {self.token}"},
        )
        return response


def test_chat_completions_are_counted_in_tokens(env: Env) -> None:
    gw = Gateway(env)
    reply = gw.chat({**CHAT, "model": "whatever-the-caller-says"})
    assert reply.status_code == 200, reply.text
    usage = reply.json()["usage"]
    sent = gw.serving.requests[-1][1]
    assert sent["model"] == "assistant-prod"  # addressed to the served model
    (call,) = gw.usage.calls
    assert (call.unit, call.units) == ("tokens", usage["total_tokens"])
    assert (call.prompt_tokens, call.completion_tokens) == (
        usage["prompt_tokens"],
        usage["completion_tokens"],
    )
    wrong = gw.client.post(
        "/v1/support/assistant-prod/predict",
        json={"instances": [[1]]},
        headers={"authorization": f"Bearer {gw.token}"},
    )
    assert wrong.status_code == 404 and "chat/completions" in wrong.json()["error"]["message"]
    assert gw.chat({"messages": []}).json()["error"]["code"] == "invalid_request"


def test_a_streamed_reply_passes_through_and_is_still_counted(env: Env) -> None:
    gw = Gateway(env)
    with gw.client.stream(
        "POST",
        "/v1/support/assistant-prod/chat/completions",
        json={**CHAT, "stream": True},
        headers={"authorization": f"Bearer {gw.token}"},
    ) as reply:
        assert reply.headers["content-type"].startswith("text/event-stream")
        events = [line for line in reply.iter_lines() if line.startswith("data: ")]
    assert events[-1] == "data: [DONE]"
    text = "".join(
        json.loads(e[6:])["choices"][0]["delta"].get("content", "")
        for e in events[:-1]
        if json.loads(e[6:])["choices"]
    )
    assert text.startswith("(demo model)")
    assert gw.serving.requests[-1][1]["stream_options"] == {"include_usage": True}
    (call,) = gw.usage.calls
    assert call.units > 0 and call.prompt_tokens > 0 and call.completion_tokens > 0


def test_token_limits_reserve_before_forwarding_and_refund_reported_usage(env: Env) -> None:
    gw = Gateway(env)  # 200 tokens per minute
    used = 0
    codes = []
    for _ in range(12):
        reply = gw.chat()
        codes.append(reply.status_code)
        if reply.status_code == 200:
            used += reply.json()["usage"]["total_tokens"]
    assert 429 in codes and codes[0] == 200
    assert 0 < used < 200  # admission reserves prompt estimate and bounded output
    refused = gw.chat()
    assert refused.status_code == 429 and int(refused.headers["retry-after"]) >= 1
    gw.time.t += 60 + CACHE_SECONDS
    assert gw.chat().status_code == 200


def test_token_meter_reads_usage_across_chunk_boundaries() -> None:
    events = (
        b'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":2}}\n\n'
        b"data: [DONE]\n\n"
    )
    meter = TokenMeter(streamed=True)
    for i in range(0, len(events), 7):  # cut anywhere, even inside a line
        meter.feed(events[i : i + 7])
    meter.finish()
    assert (meter.prompt, meter.completion, meter.total) == (11, 2, 13)
    whole = TokenMeter(streamed=False)
    whole.feed(b'{"choices": [], "usage": {"prompt_tokens": 5, ')
    whole.feed(b'"completion_tokens": 7}}')
    whole.finish()
    assert whole.total == 12
    assert json.loads(prepare_chat(json.dumps({**CHAT, "stream": True}).encode(), "m")) == {
        **CHAT,
        "model": "m",
        "stream": True,
        "stream_options": {"include_usage": True},
    }


@pytest.mark.anyio
async def test_http_upstream_sends_chats_to_the_openai_route() -> None:
    from controlplane.application.gateway import UpstreamCall

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"")

    upstream = HttpUpstream(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await upstream.call(
        UpstreamCall(
            ref="mlp-support/assistant-prod",
            url="http://assistant-prod.mlp-support.svc",
            protocol=EndpointProtocol.OPENAI,
            body=b"{}",
            timeout_seconds=5,
            request_id="r",
        )
    )
    assert seen == ["/openai/v1/chat/completions"]


def test_usage_records_carry_tokens_into_metrics() -> None:
    record = CallRecord("support", "assistant-prod", "helpdesk", 200, 30, "tokens", 0.4, 20, 10)
    assert record.prompt_tokens + record.completion_tokens == record.units


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_llm_api_flow(uow_factory: Factory, clock: Any) -> None:
    from controlplane.api.app import create_app

    projects = ProjectService(uow_factory, clock)
    project, _ = projects.create(CreateProject(name="support"))
    ProjectReconciler(uow_factory, FakeClusterProvider(), clock).reconcile(project.id)
    serving = FakeServingProvider()
    client = TestClient(create_app(uow_factory, clock, serving=serving))
    base = "/projects/support"
    created = client.post(
        f"{base}/models",
        json={
            "name": "assistant",
            "kind": "llm",
            "llm": {"gpus": 1, "context_length": 8192},
            "thresholds": {"helpfulness": {"min": 0.7}},
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["kind"] == "llm" and created.json()["llm"]["gpus"] == 1
    bad = client.post(f"{base}/models", json={"name": "x-model", "llm": {"gpus": 1}})
    assert bad.status_code == 422
    version = client.post(
        f"{base}/models/assistant/versions", json={"source": QWEN, "metrics": {"helpfulness": 0.8}}
    )
    assert version.status_code == 201 and version.json()["source_uri"] == QWEN
    again = client.post(f"{base}/models/assistant/versions", json={"source": QWEN})
    assert again.status_code == 200
    evaluated = client.post(f"/model-versions/{version.json()['id']}/evaluate")
    assert evaluated.json()["status"] == "CANDIDATE"
    client.post(f"{base}/deployments", json={"name": "assistant-prod"})
    refused = client.post(
        f"{base}/deployments/assistant-prod/revisions", json={"model": "assistant", "version": 1}
    )
    assert refused.status_code == 409 and "GPU" in refused.json()["error"]["message"]
    quota = client.put(f"{base}/gpu-quota", json={"gpus": 2})
    assert quota.status_code == 200 and quota.json()["gpu_quota"] == 2
    deployed = client.post(
        f"{base}/deployments/assistant-prod/revisions", json={"model": "assistant", "version": 1}
    )
    assert deployed.status_code == 202, deployed.text
    assert deployed.json()["revisions"][0]["runtime"] == "huggingface"
    DeploymentReconciler(uow_factory, serving, clock).reconcile_all()
    summary = client.get(f"{base}/summary").json()
    assert (summary["gpu_quota"], summary["gpus_in_use"]) == (2, 1)
    answer = client.post(
        f"{base}/endpoints/assistant-prod/chat",
        json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 64},
    )
    assert answer.status_code == 200 and answer.json()["usage"]["total_tokens"] > 0
    assert serving.requests[-1][1]["stream"] is False
    assert (
        client.post(
            f"{base}/endpoints/assistant-prod/chat",
            json={"messages": [{"role": "robot", "content": ""}]},
        ).status_code
        == 422
    )
    access = client.get(f"{base}/endpoints/assistant-prod/access").json()
    assert (access["kind"], access["protocol"], access["operation"]) == (
        "llm",
        "openai",
        "chat/completions",
    )
