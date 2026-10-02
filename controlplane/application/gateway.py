"""The inference gateway's logic: who is calling, may they, is there room, forward, count.

The gateway is the data plane: it runs apart from the control plane's API so prediction
traffic never competes with management calls, and it is the only public way in. It reads
endpoints and keys from the platform database through a short cache, and forwards to the
serving system directly.

Nothing here knows HTTP frameworks. The ASGI app in controlplane/gateway turns requests
into `invoke()` calls and `GatewayReply` / `GatewayError` back into responses.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from controlplane.application.deployments import serving_ref
from controlplane.application.identity import Authenticator, role_in
from controlplane.application.llm import InvalidChatRequest, TokenMeter, prepare_chat
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.domain.access import Principal, ProjectRole
from controlplane.domain.api_keys import ApiKey, parse_token
from controlplane.domain.entities import EndpointLimits
from controlplane.domain.errors import Unauthenticated
from controlplane.domain.states import (
    EndpointKind,
    EndpointProtocol,
    EndpointStatus,
    Exposure,
)

CACHE_SECONDS = 5.0  # how stale an endpoint or key may be (a revoked key works this long)
TOUCH_SECONDS = 60.0  # last_used_at is written at most this often per key

# The operation each protocol answers, as the last part of the public path.
OPERATIONS: dict[EndpointProtocol, str] = {
    EndpointProtocol.V2_INFER: "predict",
    EndpointProtocol.OPENAI: "chat/completions",
    EndpointProtocol.HTTP: "invoke",
}
UNIT: dict[EndpointKind, str] = {
    EndpointKind.MODEL: "requests",
    EndpointKind.LLM: "tokens",
    EndpointKind.FUNCTION: "requests",
}


class GatewayError(Exception):
    """A refusal or a failure, with the HTTP status and the public error code."""

    def __init__(
        self, status: int, code: str, message: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.headers = headers or {}


# -- ports -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UpstreamCall:
    ref: str  # the serving resource ("namespace/name")
    url: str | None  # its address inside the cluster
    protocol: EndpointProtocol
    body: bytes
    timeout_seconds: float
    request_id: str


@dataclass(frozen=True, slots=True)
class UpstreamReply:
    status: int
    content_type: str
    chunks: AsyncIterator[bytes]  # streamed through as they arrive


class Upstream(Protocol):
    async def call(self, call: UpstreamCall) -> UpstreamReply:
        """Raise TimeoutError past the timeout and ConnectionError when nothing answered."""
        ...


@dataclass(frozen=True, slots=True)
class Allowance:
    allowed: bool
    limit: int  # the tighter of the limits that applied, per minute
    remaining: int
    reset_seconds: int  # until the bucket is full again (or, when refused, until it fits)


class RateLimiter(Protocol):
    def take(self, buckets: Sequence[tuple[str, int]], units: int) -> Allowance:
        """Take `units` from every (bucket, per-minute limit) at once, or from none."""
        ...

    def admit(self, buckets: Sequence[tuple[str, int]]) -> Allowance:
        """Allowed while every bucket has something left; takes nothing. For calls whose
        cost (tokens) is only known once they have been answered."""
        ...

    def charge(self, buckets: Sequence[tuple[str, int]], units: int) -> None:
        """Take what a call turned out to cost, even into debt: the next calls wait."""
        ...


@dataclass(frozen=True, slots=True)
class CallRecord:
    project: str
    endpoint: str
    caller: str  # a key's name, or "user:<name>" for a signed-in caller
    status: int
    units: float
    unit: str
    seconds: float
    prompt_tokens: int = 0  # LLMs: what the units were made of
    completion_tokens: int = 0


class UsageRecorder(Protocol):
    def record(self, call: CallRecord) -> None: ...


# -- what a call resolves to ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Route:
    project_id: UUID
    project: str
    endpoint: str
    status: EndpointStatus
    kind: EndpointKind
    protocol: EndpointProtocol
    limits: EndpointLimits
    ref: str
    url: str | None
    model: str | None  # "scorer v3" when one version serves all traffic


@dataclass(frozen=True, slots=True)
class Caller:
    name: str  # what usage is counted under
    key: ApiKey | None = None
    principal: Principal | None = None


@dataclass(frozen=True, slots=True)
class GatewayReply:
    status: int
    content_type: str
    chunks: AsyncIterator[bytes]
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class _Cached[T]:
    value: T
    at: float


class GatewayService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        upstream: Upstream,
        limiter: RateLimiter,
        recorders: Sequence[UsageRecorder] = (),
        authenticator: Authenticator | None = None,
        clock: Clock = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._uow_factory = uow_factory
        self._upstream = upstream
        self._limiter = limiter
        self._recorders = recorders
        self._authenticator = authenticator
        self._clock = clock
        self._monotonic = monotonic
        self._routes: dict[tuple[str, str], _Cached[Route | None]] = {}
        self._keys: dict[str, _Cached[ApiKey | None]] = {}
        self._touched: dict[str, float] = {}

    async def invoke(
        self,
        *,
        token: str | None,
        project: str,
        endpoint: str,
        operation: str,
        body: bytes,
        request_id: str,
    ) -> GatewayReply:
        started = self._monotonic()
        caller: Caller | None = None
        route: Route | None = None
        try:
            caller = self._authenticate(token)
            route = self._route(project, endpoint)
            if route is None:
                # Missing and internal look the same, so the gateway does not reveal what exists.
                raise GatewayError(404, "not_found", f"no public endpoint {project}/{endpoint}")
            self._authorize(caller, route)
            expected = OPERATIONS[route.protocol]
            if operation != expected:
                raise GatewayError(
                    404,
                    "not_found",
                    f"this endpoint speaks {route.protocol.value}: "
                    f"POST /v1/{project}/{endpoint}/{expected}",
                )
            if route.status is not EndpointStatus.READY:
                raise GatewayError(409, "not_ready", f"endpoint is {route.status.value}, not READY")
            if len(body) > route.limits.max_body_kb * 1024:
                raise GatewayError(
                    413, "too_large", f"the body may be at most {route.limits.max_body_kb} KB"
                )
            if route.protocol is EndpointProtocol.OPENAI:
                try:
                    body = prepare_chat(body, route.endpoint)
                except InvalidChatRequest as exc:
                    raise GatewayError(400, "invalid_request", str(exc)) from exc
            allowance = self._limit(caller, route)
            reply = await self._forward(route, body, request_id)
        except GatewayError as error:
            self._record(caller, route, error.status, 0, started)
            raise
        headers = {
            "RateLimit-Limit": str(allowance.limit),
            "RateLimit-Remaining": str(allowance.remaining),
            "RateLimit-Reset": str(allowance.reset_seconds),
        }
        if route.model:
            headers["X-MLP-Model"] = route.model
        return GatewayReply(
            reply.status,
            reply.content_type,
            self._counted(reply, caller, route, started),
            headers,
        )

    # -- steps -------------------------------------------------------------------------------

    def _authenticate(self, token: str | None) -> Caller:
        if not token:
            raise GatewayError(401, "unauthenticated", "send an API key: Authorization: Bearer ...")
        parsed = parse_token(token)
        if parsed is not None:
            key_id, secret = parsed
            key = self._key(key_id)
            if key is None or not key.matches(secret):
                raise GatewayError(401, "unauthenticated", "unknown API key")
            if not key.usable(self._clock()):
                raise GatewayError(401, "unauthenticated", "this API key is revoked or expired")
            return Caller(name=key.name, key=key)
        if self._authenticator is None:
            raise GatewayError(401, "unauthenticated", "not an API key")
        try:
            principal = self._authenticator.authenticate(token)
        except Unauthenticated as exc:
            raise GatewayError(401, "unauthenticated", str(exc)) from exc
        return Caller(name=f"user:{principal.username}", principal=principal)

    def _authorize(self, caller: Caller, route: Route) -> None:
        if caller.key is not None:
            if caller.key.project_id != route.project_id or not caller.key.may_call(route.endpoint):
                raise GatewayError(403, "forbidden", "this key may not call this endpoint")
            self._touch(caller.key)
            return
        assert caller.principal is not None
        with self._uow_factory() as uow:
            role = role_in(uow, route.project_id, caller.principal)
        if role is None or not role.includes(ProjectRole.INVOKER):
            raise GatewayError(403, "forbidden", "you need the invoker role in this project")

    @staticmethod
    def _buckets(caller: Caller, route: Route) -> list[tuple[str, int]]:
        own = caller.key.units_per_minute if caller.key else None
        return [
            (f"endpoint:{route.project}/{route.endpoint}", route.limits.units_per_minute),
            (
                f"caller:{route.project}/{caller.name}/{route.endpoint}",
                own or route.limits.units_per_minute,
            ),
        ]

    def _limit(self, caller: Caller, route: Route) -> Allowance:
        buckets = self._buckets(caller, route)
        if route.kind is EndpointKind.LLM:
            # Tokens are known only once the model has answered: let the call in while there
            # is room, charge what it used afterwards.
            allowance = self._limiter.admit(buckets)
        else:
            allowance = self._limiter.take(buckets, 1)
        if not allowance.allowed:
            raise GatewayError(
                429,
                "rate_limited",
                f"over {allowance.limit} units per minute",
                {"Retry-After": str(max(1, allowance.reset_seconds))},
            )
        return allowance

    async def _forward(self, route: Route, body: bytes, request_id: str) -> UpstreamReply:
        call = UpstreamCall(
            ref=route.ref,
            url=route.url,
            protocol=route.protocol,
            body=body,
            timeout_seconds=route.limits.timeout_seconds,
            request_id=request_id,
        )
        try:
            return await self._upstream.call(call)
        except TimeoutError as exc:
            raise GatewayError(
                504, "timeout", f"no answer within {route.limits.timeout_seconds}s"
            ) from exc
        except ConnectionError as exc:
            raise GatewayError(502, "upstream_unavailable", "the model did not answer") from exc

    async def _counted(
        self, reply: UpstreamReply, caller: Caller, route: Route, started: float
    ) -> AsyncIterator[bytes]:
        """Pass the reply through as it arrives, then count the call once it has ended: one
        request for a model, the tokens it used for an LLM."""
        status = reply.status
        meter = None
        if route.kind is EndpointKind.LLM:
            meter = TokenMeter(streamed="text/event-stream" in reply.content_type)
        try:
            async for chunk in reply.chunks:
                if meter is not None:
                    meter.feed(chunk)
                yield chunk
        except Exception:
            status = 502
            raise
        finally:
            if meter is None:
                self._record(caller, route, status, 1 if status < 500 else 0, started)
            else:
                meter.finish()
                if meter.total:
                    self._limiter.charge(self._buckets(caller, route), meter.total)
                self._record(
                    caller, route, status, meter.total, started, meter.prompt, meter.completion
                )

    def _record(
        self,
        caller: Caller | None,
        route: Route | None,
        status: int,
        units: float,
        started: float,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        # Unknown endpoints and anonymous callers fold into one label value each: a metric
        # label never carries a string an outsider chose.
        record = CallRecord(
            project=route.project if route else "(unknown)",
            endpoint=route.endpoint if route else "(unknown)",
            caller=caller.name if caller else "(anonymous)",
            status=status,
            units=units,
            unit=UNIT[route.kind] if route else "requests",
            seconds=self._monotonic() - started,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        for recorder in self._recorders:
            recorder.record(record)

    # -- lookups, cached ---------------------------------------------------------------------

    def _fresh[T](self, entry: _Cached[T] | None) -> bool:
        return entry is not None and self._monotonic() - entry.at < CACHE_SECONDS

    def _route(self, project: str, endpoint: str) -> Route | None:
        cached = self._routes.get((project, endpoint))
        if cached is not None and self._fresh(cached):
            return cached.value
        route = self._load_route(project, endpoint)
        self._routes[(project, endpoint)] = _Cached(route, self._monotonic())
        return route

    def _load_route(self, project_name: str, name: str) -> Route | None:
        with self._uow_factory() as uow:
            project = uow.projects.get_by_name(project_name)
            if project is None:
                return None
            endpoint = uow.endpoints.get_by_name(project.id, name)
            if endpoint is None or endpoint.exposure is not Exposure.PUBLIC:
                return None
            deployment = uow.deployments.get(endpoint.deployment_id)
            assert deployment is not None
            model = None
            stable = deployment.active_revision
            if stable is not None and uow.rollouts.get_active(deployment.id) is None:
                revision = uow.revisions.get(deployment.id, stable)
                version = uow.model_versions.get(revision.model_version_id) if revision else None
                owner = uow.models.get(version.model_id) if version else None
                if version is not None and owner is not None:
                    model = f"{owner.name} v{version.version}"
            return Route(
                project_id=project.id,
                project=project.name,
                endpoint=endpoint.name,
                status=endpoint.status,
                kind=endpoint.kind,
                protocol=endpoint.protocol,
                limits=endpoint.limits,
                ref=serving_ref(project, deployment),
                url=endpoint.url,
                model=model,
            )

    def _key(self, key_id: str) -> ApiKey | None:
        cached = self._keys.get(key_id)
        if cached is not None and self._fresh(cached):
            return cached.value
        with self._uow_factory() as uow:
            key = uow.api_keys.get(key_id)
        self._keys[key_id] = _Cached(key, self._monotonic())
        return key

    def _touch(self, key: ApiKey) -> None:
        """Remember when a key was last used, without a database write per request."""
        now = self._monotonic()
        if now - self._touched.get(key.key_id, -TOUCH_SECONDS) < TOUCH_SECONDS:
            return
        self._touched[key.key_id] = now
        with self._uow_factory() as uow:
            current = uow.api_keys.get(key.key_id)
            if current is not None:
                uow.api_keys.update(current.used(self._clock()))
                uow.commit()
