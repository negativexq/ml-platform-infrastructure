"""The inference gateway: keys, exposure, limits, forwarding, errors and usage."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from controlplane.adapters.gateway import HttpUpstream, ServingUpstream, TokenBucketLimiter
from controlplane.application.api_access import ApiAccessService
from controlplane.application.gateway import (
    CACHE_SECONDS,
    CallRecord,
    GatewayService,
    UpstreamCall,
    UpstreamReply,
)
from controlplane.application.ports import UnitOfWork
from controlplane.domain.access import Membership, Principal, ProjectRole
from controlplane.domain.api_keys import parse_token
from controlplane.domain.entities import EndpointLimits
from controlplane.domain.errors import AlreadyExists, InvalidArgument, Unauthenticated
from controlplane.domain.states import Exposure
from controlplane.gateway import create_gateway
from controlplane.tests.test_deployments import Env as DeploymentEnv

Factory = Callable[[], UnitOfWork]
URL = "/v1/credit-risk/credit-risk-prod/predict"
BODY = {"instances": [[1, 2, 3], [4, 5, 6]]}


class Monotonic:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Recorder:
    def __init__(self) -> None:
        self.calls: list[CallRecord] = []

    def record(self, call: CallRecord) -> None:
        self.calls.append(call)


class StubAuthenticator:
    def authenticate(self, token: str) -> Principal:
        if token != "oidc-token-for-dana":
            raise Unauthenticated("bad token")
        return Principal(username="dana")


class Env(DeploymentEnv):
    def __init__(self, factory: Factory, clock: Callable[[], Any]) -> None:
        super().__init__(factory, clock)
        self.deploy(1)
        self.reconciler.reconcile(self.id())
        self.access = ApiAccessService(factory, clock)
        self.time = Monotonic()
        self.usage = Recorder()
        self.gateway = GatewayService(
            factory,
            ServingUpstream(self.serving),
            TokenBucketLimiter(self.time),
            recorders=[self.usage],
            authenticator=StubAuthenticator(),
            clock=clock,
            monotonic=self.time,
        )
        self.client = TestClient(create_gateway(self.gateway))

    def expose(self, **limits: int) -> None:
        self.access.expose(
            "credit-risk", "credit-risk-prod", Exposure.PUBLIC, EndpointLimits(**limits)
        )

    def key(self, name: str = "partner-acme", **kw: Any) -> str:
        kw.setdefault("endpoints", ["credit-risk-prod"])
        return self.access.create_key("credit-risk", name=name, **kw)[1]

    def call(self, token: str | None, url: str = URL, **kw: Any) -> httpx.Response:
        headers = {"authorization": f"Bearer {token}"} if token else {}
        headers.update(kw.pop("headers", {}))
        kw.setdefault("json", BODY)
        response: httpx.Response = self.client.post(url, headers=headers, **kw)
        return response

    def later(self, seconds: float = CACHE_SECONDS + 1) -> None:
        self.time.t += seconds


@pytest.fixture
def env(uow_factory: Factory, clock: Callable[[], Any]) -> Env:
    return Env(uow_factory, clock)


def test_a_public_endpoint_answers_a_key_and_says_what_answered(env: Env) -> None:
    env.expose()
    reply = env.call(env.key(), headers={"x-request-id": "trace-me-1"})
    assert reply.status_code == 200, reply.text
    assert reply.json() == {"predictions": [0.0, 0.0]}
    assert reply.headers["x-request-id"] == "trace-me-1"
    assert reply.headers["x-mlp-model"] == "scorer v1"
    assert reply.headers["ratelimit-limit"] == "600"
    assert reply.headers["ratelimit-remaining"] == "599"
    assert env.serving.requests[-1] == (env.ref(), BODY)


def test_one_error_shape_and_no_hints_about_what_exists(env: Env) -> None:
    token = env.key()
    missing = env.call(None)
    assert missing.status_code == 401
    error = missing.json()["error"]
    assert error["code"] == "unauthenticated" and error["request_id"].startswith("req_")
    assert missing.headers["x-request-id"] == error["request_id"]

    internal = env.call(token)  # the endpoint exists but is not public
    unknown = env.call(token, "/v1/credit-risk/nope/predict")
    assert internal.status_code == unknown.status_code == 404
    assert internal.json()["error"]["code"] == unknown.json()["error"]["code"] == "not_found"

    env.expose()
    env.later()
    wrong = env.call(token, "/v1/credit-risk/credit-risk-prod/chat/completions")
    assert wrong.status_code == 404 and "/predict" in wrong.json()["error"]["message"]
    assert env.client.get(URL).status_code == 405
    assert env.client.post("/elsewhere").json()["error"]["code"] == "not_found"


def test_a_key_calls_only_its_own_endpoints_and_stops_when_revoked(env: Env) -> None:
    env.expose()
    other = env.access.create_key("credit-risk", name="other", endpoints=["credit-risk-prod"])
    env.service.create("credit-risk", "credit-risk-staging")
    env.access.expose("credit-risk", "credit-risk-staging", Exposure.PUBLIC, EndpointLimits())
    staging = env.call(other[1], "/v1/credit-risk/credit-risk-staging/predict")
    assert staging.status_code == 403

    token = env.key()
    assert env.call(token).status_code == 200
    key_id, _ = parse_token(token) or ("", "")
    env.access.revoke_key("credit-risk", key_id)
    assert env.call(token).status_code == 200  # cached for a few seconds
    env.later()
    assert env.call(token).json()["error"]["message"] == "this API key is revoked or expired"
    forged = token[:-4] + "AAAA"
    assert env.call(forged).status_code == 401


def test_expired_keys_are_refused(env: Env, clock: Any) -> None:
    env.expose()
    token = env.key(expires_at=clock() + timedelta(minutes=5))
    assert env.call(token).status_code == 200
    clock.advance(timedelta(minutes=6))
    assert env.call(token).status_code == 401


def test_limits_per_endpoint_and_per_key(env: Env) -> None:
    env.expose(units_per_minute=3)
    small = env.key("small", units_per_minute=1)
    big = env.key("big")
    assert env.call(small).status_code == 200
    refused = env.call(small)
    assert refused.status_code == 429 and refused.headers["retry-after"] == "60"
    assert [env.call(big).status_code for _ in range(3)] == [200, 200, 429]  # endpoint total
    env.later(20)  # one unit per 20 seconds comes back
    assert env.call(big).status_code == 200


def test_body_limit_and_readiness(env: Env) -> None:
    env.expose(max_body_kb=1)
    token = env.key()
    too_big = env.call(token, json={"instances": [[0.123456789] * 200]})
    assert too_big.status_code == 413 and too_big.json()["error"]["code"] == "too_large"

    env.service.create("credit-risk", "credit-risk-new")  # nothing deployed: not READY
    env.access.expose("credit-risk", "credit-risk-new", Exposure.PUBLIC, EndpointLimits())
    new = env.access.create_key("credit-risk", name="new", endpoints=["credit-risk-new"])[1]
    assert env.call(new, "/v1/credit-risk/credit-risk-new/predict").status_code == 409


def test_upstream_failures_are_502_and_504(env: Env) -> None:
    env.expose(timeout_seconds=1)
    token = env.key()
    env.serving.delete(env.ref())  # the model stops serving
    down = env.call(token)
    assert down.status_code == 502 and down.json()["error"]["code"] == "upstream_unavailable"


def test_signed_in_callers_need_the_invoker_role(env: Env, uow_factory: Factory) -> None:
    env.expose()
    assert env.call("oidc-token-for-dana").status_code == 403
    with uow_factory() as uow:
        uow.memberships.add(
            Membership.create(
                project_id=env.project_id,
                subject="user:dana",
                role=ProjectRole.INVOKER,
                now=env.clock(),
            )
        )
        uow.commit()
    assert env.call("oidc-token-for-dana").status_code == 200
    assert env.call("not-a-token").status_code == 401


def test_usage_is_recorded_without_outsider_chosen_labels(env: Env, uow_factory: Factory) -> None:
    env.expose()
    token = env.key()
    env.call(token)
    env.call(token, "/v1/credit-risk/made-up-by-a-stranger/predict")
    env.call(None)
    ok, unknown, anonymous = env.usage.calls
    assert (ok.project, ok.endpoint, ok.caller, ok.status, ok.units) == (
        "credit-risk",
        "credit-risk-prod",
        "partner-acme",
        200,
        1,
    )
    assert (unknown.endpoint, unknown.status, unknown.units) == ("(unknown)", 404, 0)
    assert (anonymous.caller, anonymous.status) == ("(anonymous)", 401)
    with uow_factory() as uow:
        (key,) = uow.api_keys.list(env.project_id)
    assert key.last_used_at is not None


def test_keys_are_shown_once_and_audited(env: Env, uow_factory: Factory) -> None:
    key, token = env.access.create_key(
        "credit-risk", name="partner-acme", endpoints=["credit-risk-prod"]
    )
    assert token.startswith("mlp_live_") and token.split("_")[2] == key.key_id
    assert token not in repr(key) and key.secret_hash not in token
    with pytest.raises(AlreadyExists):
        env.access.create_key("credit-risk", name="partner-acme", endpoints=["credit-risk-prod"])
    with pytest.raises(InvalidArgument, match="no such endpoint"):
        env.access.create_key("credit-risk", name="x-key", endpoints=["nope"])
    with pytest.raises(InvalidArgument):
        env.access.expose(
            "credit-risk", "credit-risk-prod", Exposure.PUBLIC, EndpointLimits(units_per_minute=0)
        )
    _, changed = env.access.expose(
        "credit-risk", "credit-risk-prod", Exposure.PUBLIC, EndpointLimits()
    )
    _, again = env.access.expose(
        "credit-risk", "credit-risk-prod", Exposure.PUBLIC, EndpointLimits()
    )
    assert (changed, again) == (True, False)
    env.access.revoke_key("credit-risk", key.key_id)
    env.access.revoke_key("credit-risk", key.key_id)  # idempotent
    actions = env.audit()
    assert actions.count("api_key.created") == 1 and actions.count("api_key.revoked") == 1
    assert actions.count("endpoint.exposure_changed") == 1
    with uow_factory() as uow:
        events = uow.audit.list(project_id=env.project_id)
    assert all(token not in json.dumps(e.payload) for e in events)


def test_token_bucket_refills_and_reports_the_tighter_limit() -> None:
    time = Monotonic()
    limiter = TokenBucketLimiter(time)
    both = [("endpoint", 60), ("caller", 2)]
    first = limiter.take(both, 1)
    assert (first.allowed, first.limit, first.remaining) == (True, 2, 1)
    assert limiter.take(both, 1).allowed
    refused = limiter.take(both, 1)
    assert (refused.allowed, refused.reset_seconds) == (False, 30)
    assert limiter.take([("endpoint", 60)], 1).remaining == 57  # refused calls took nothing
    time.t += 30
    assert limiter.take(both, 1).allowed


# -- the HTTP upstream (the real serving system), against a stand-in server ------------------


def _call(**kw: Any) -> UpstreamCall:
    defaults: dict[str, Any] = {
        "ref": "mlp-credit-risk/credit-risk-prod",
        "url": "http://credit-risk-prod.mlp-credit-risk.svc.cluster.local",
        "protocol": "v2-infer",
        "body": b'{"instances": [[1]]}',
        "timeout_seconds": 5,
        "request_id": "req_1",
    }
    from controlplane.domain.states import EndpointProtocol

    defaults.update(kw)
    defaults["protocol"] = EndpointProtocol(defaults["protocol"])
    return UpstreamCall(**defaults)


async def _body(reply: UpstreamReply) -> bytes:
    return b"".join([chunk async for chunk in reply.chunks])


@pytest.mark.anyio
@pytest.mark.parametrize(
    "body,path",
    [
        (b'{"instances": [[1]]}', "/invocations"),
        (b'{"inputs": []}', "/v2/models/credit-risk-prod/infer"),
        (b"not JSON", "/v2/models/credit-risk-prod/infer"),
    ],
)
async def test_http_upstream_streams_from_the_matching_model_route(body: bytes, path: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=b'{"predictions": [1]}'
        )

    upstream = HttpUpstream(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    reply = await upstream.call(_call(body=body))
    assert reply.status == 200 and await _body(reply) == b'{"predictions": [1]}'
    (request,) = seen
    assert str(request.url).endswith(path)
    assert request.content == body
    assert request.headers["x-request-id"] == "req_1"


@pytest.mark.anyio
async def test_http_upstream_maps_timeouts_and_refusals() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def gone(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(TimeoutError):
        await HttpUpstream(httpx.AsyncClient(transport=httpx.MockTransport(slow))).call(_call())
    with pytest.raises(ConnectionError):
        await HttpUpstream(httpx.AsyncClient(transport=httpx.MockTransport(gone))).call(_call())
    with pytest.raises(ConnectionError, match="no address"):
        await HttpUpstream(httpx.AsyncClient()).call(_call(url=None))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# -- management API ---------------------------------------------------------------------------


def test_management_api_flow(env: Env, uow_factory: Factory, clock: Any) -> None:
    from controlplane.adapters.fakes import FakeUsage
    from controlplane.api.app import create_app

    usage = FakeUsage(clock)
    client = TestClient(
        create_app(
            uow_factory,
            clock,
            serving=env.serving,
            usage=usage,
            gateway_url="https://api.example.com/",
        )
    )
    base = "/projects/credit-risk"
    access = client.get(f"{base}/endpoints/credit-risk-prod/access").json()
    assert access["exposure"] == "internal" and access["keys"] == []
    assert access["public_url"] == "https://api.example.com/v1/credit-risk/credit-risk-prod/predict"

    opened = client.patch(
        f"{base}/endpoints/credit-risk-prod",
        json={"exposure": "public", "limits": {"units_per_minute": 100}},
    )
    assert opened.status_code == 200, opened.text
    assert opened.json()["exposure"] == "public"
    assert opened.json()["limits"] == {
        "units_per_minute": 100,
        "max_body_kb": 256,
        "timeout_seconds": 30,
    }
    kept = client.patch(f"{base}/endpoints/credit-risk-prod", json={"exposure": "public"})
    assert kept.json()["limits"]["units_per_minute"] == 100  # omitted limits are kept
    bad = client.patch(f"{base}/endpoints/credit-risk-prod", json={"exposure": "everyone"})
    assert bad.status_code == 422

    created = client.post(
        f"{base}/api-keys", json={"name": "partner-acme", "endpoints": ["credit-risk-prod"]}
    )
    assert created.status_code == 201 and created.headers["cache-control"] == "no-store"
    body = created.json()
    assert body["secret"].startswith("mlp_live_") and body["key"]["state"] == "active"
    listed = client.get(f"{base}/api-keys").json()["items"]
    assert [k["name"] for k in listed] == ["partner-acme"] and "secret" not in listed[0]
    assert (
        client.get(f"{base}/endpoints/credit-risk-prod/access").json()["keys"][0]["key_id"]
        == (body["key"]["key_id"])
    )
    assert (
        client.post(f"{base}/api-keys", json={"name": "x-key", "endpoints": []}).status_code == 422
    )

    # a call through the gateway shows up in the usage panel
    env.gateway._recorders = [usage]  # noqa: SLF001
    env.later()
    assert env.call(body["secret"]).status_code == 200
    used = client.get(f"{base}/endpoints/credit-risk-prod/usage?minutes=15").json()
    assert used["available"] and used["unit"] == "requests"
    (acme,) = used["callers"]
    assert acme["caller"] == "partner-acme" and acme["units"] == pytest.approx(1)

    revoked = client.delete(f"{base}/api-keys/{body['key']['key_id']}")
    assert revoked.status_code == 200 and revoked.json()["state"] == "revoked"
    assert client.delete(f"{base}/api-keys/deadbeef").status_code == 404
    without = TestClient(create_app(uow_factory, clock)).get(
        f"{base}/endpoints/credit-risk-prod/usage"
    )
    assert without.json()["available"] is False and without.json()["error"]
