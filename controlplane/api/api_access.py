"""Opening endpoints to the outside: exposure and limits, API keys, usage."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from controlplane.api.errors import PlatformRoute
from controlplane.api.schemas import ErrorOut
from controlplane.api.schemas_deployments import EndpointLimitsBody, EndpointOut
from controlplane.application.api_access import ApiAccessService, EndpointUsage
from controlplane.application.gateway import OPERATIONS
from controlplane.application.projects import Clock
from controlplane.domain.api_keys import ApiKey
from controlplane.domain.entities import EndpointLimits
from controlplane.domain.states import EndpointKind, EndpointProtocol, Exposure

_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorOut},
    409: {"model": ErrorOut},
    422: {"model": ErrorOut},
}


class EndpointExposure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exposure: Exposure = Field(description="public: callable through the gateway with a key")
    limits: EndpointLimitsBody | None = Field(None, description="omit to keep the current ones")


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="who it is for, e.g. partner-acme")
    endpoints: list[str] = Field(min_length=1, description="endpoint names this key may call")
    units_per_minute: int | None = Field(
        None, ge=1, le=1_000_000, description="this key's own limit; null: the endpoint's"
    )
    expires_at: datetime | None = None


class ApiKeyOut(BaseModel):
    key_id: str = Field(description="the public part of the key, safe to show and log")
    name: str
    endpoints: list[str]
    units_per_minute: int | None
    state: str = Field(description="active, revoked or expired")
    created_by: str
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None

    @classmethod
    def from_domain(cls, k: ApiKey, now: datetime) -> ApiKeyOut:
        state = "revoked" if k.revoked_at else "active" if k.usable(now) else "expired"
        return cls(
            key_id=k.key_id,
            name=k.name,
            endpoints=list(k.endpoints),
            units_per_minute=k.units_per_minute,
            state=state,
            created_by=k.created_by,
            created_at=k.created_at,
            expires_at=k.expires_at,
            revoked_at=k.revoked_at,
            last_used_at=k.last_used_at,
        )


class ApiKeyCreated(BaseModel):
    key: ApiKeyOut
    secret: str = Field(description="the full key; shown this once and never again")


class ApiKeyList(BaseModel):
    items: list[ApiKeyOut]


class EndpointAccessOut(BaseModel):
    endpoint: str
    kind: EndpointKind
    protocol: EndpointProtocol
    exposure: Exposure
    limits: EndpointLimitsBody
    operation: str = Field(description="the last part of the public path, e.g. predict")
    public_url: str | None = Field(description="null when no gateway URL is configured")
    keys: list[ApiKeyOut] = Field(description="keys that may call this endpoint")


class UsagePointOut(BaseModel):
    at: datetime
    units: float = Field(description="per minute")
    rejected: float = Field(description="per minute, refused by the gateway (4xx)")
    errors: float = Field(description="per minute, failed upstream (5xx)")


class CallerUsageOut(BaseModel):
    caller: str
    units: float = Field(description="in the window")
    rejected: float
    errors: float
    prompt_tokens: float | None = Field(None, description="LLMs: tokens sent, in the window")
    completion_tokens: float | None = Field(None, description="LLMs: tokens generated")
    points: list[UsagePointOut]


class SampleValueOut(BaseModel):
    at: datetime
    value: float


class EndpointUsageOut(BaseModel):
    available: bool
    error: str | None
    unit: str = Field(description="what quotas count: requests (models) or tokens (LLMs)")
    start: datetime
    end: datetime
    step_seconds: int
    callers: list[CallerUsageOut]
    p95_latency_ms: list[SampleValueOut]

    @classmethod
    def from_view(cls, u: EndpointUsage) -> EndpointUsageOut:
        return cls(
            available=u.available,
            error=u.error,
            unit=u.unit,
            start=u.start,
            end=u.end,
            step_seconds=u.step_seconds,
            callers=[
                CallerUsageOut(
                    caller=c.caller,
                    units=c.units,
                    rejected=c.rejected,
                    errors=c.errors,
                    prompt_tokens=c.prompt_tokens,
                    completion_tokens=c.completion_tokens,
                    points=[
                        UsagePointOut(at=p.at, units=p.units, rejected=p.rejected, errors=p.errors)
                        for p in c.points
                    ],
                )
                for c in u.callers
            ],
            p95_latency_ms=[SampleValueOut(at=s.at, value=s.value) for s in u.p95_latency_ms],
        )


def api_access_router() -> APIRouter:
    router = APIRouter(route_class=PlatformRoute, prefix="/projects/{project}", tags=["api access"])

    def svc(request: Request) -> ApiAccessService:
        service: ApiAccessService = request.app.state.api_access
        return service

    def now(request: Request) -> datetime:
        clock: Clock = request.app.state.clock
        return clock()

    @router.get(
        "/endpoints/{name}/access",
        response_model=EndpointAccessOut,
        responses=_ERRORS,
        summary="How an endpoint is reached from outside: exposure, limits, URL, keys",
    )
    def get_access(project: str, name: str, request: Request) -> EndpointAccessOut:
        access = svc(request).access(project, name)
        e = access.endpoint
        operation = OPERATIONS[e.protocol]
        gateway: str | None = request.app.state.gateway_url
        at = now(request)
        return EndpointAccessOut(
            endpoint=e.name,
            kind=e.kind,
            protocol=e.protocol,
            exposure=e.exposure,
            limits=EndpointOut.from_domain(e).limits,
            operation=operation,
            public_url=f"{gateway}/v1/{access.project}/{e.name}/{operation}" if gateway else None,
            keys=[ApiKeyOut.from_domain(k, at) for k in access.keys],
        )

    @router.patch(
        "/endpoints/{name}",
        response_model=EndpointOut,
        responses=_ERRORS,
        summary="Open an endpoint to the gateway (public) or close it (internal), with limits",
    )
    def set_exposure(
        project: str, name: str, body: EndpointExposure, request: Request
    ) -> EndpointOut:
        current = svc(request).access(project, name).endpoint
        limits = (
            EndpointLimits(**body.limits.model_dump())
            if body.limits is not None
            else current.limits
        )
        endpoint, _ = svc(request).expose(project, name, body.exposure, limits)
        return EndpointOut.from_domain(endpoint)

    @router.get(
        "/endpoints/{name}/usage",
        response_model=EndpointUsageOut,
        responses=_ERRORS,
        summary="Calls through the gateway, by caller",
    )
    def get_usage(
        project: str,
        name: str,
        request: Request,
        minutes: Annotated[int, Query(ge=5, le=1440)] = 60,
    ) -> EndpointUsageOut:
        return EndpointUsageOut.from_view(svc(request).usage(project, name, minutes=minutes))

    @router.get("/api-keys", response_model=ApiKeyList, responses=_ERRORS, summary="List API keys")
    def list_keys(project: str, request: Request) -> ApiKeyList:
        at = now(request)
        return ApiKeyList(
            items=[ApiKeyOut.from_domain(k, at) for k in svc(request).list_keys(project)]
        )

    @router.post(
        "/api-keys",
        response_model=ApiKeyCreated,
        status_code=status.HTTP_201_CREATED,
        responses=_ERRORS,
        summary="Issue an API key (the secret is in this response only)",
    )
    def create_key(
        project: str, body: ApiKeyCreate, request: Request, response: Response
    ) -> ApiKeyCreated:
        key, secret = svc(request).create_key(
            project,
            name=body.name,
            endpoints=body.endpoints,
            units_per_minute=body.units_per_minute,
            expires_at=body.expires_at,
        )
        response.headers["Cache-Control"] = "no-store"
        return ApiKeyCreated(key=ApiKeyOut.from_domain(key, now(request)), secret=secret)

    @router.delete(
        "/api-keys/{key_id}",
        response_model=ApiKeyOut,
        responses=_ERRORS,
        summary="Revoke an API key (idempotent; the gateway stops accepting it within seconds)",
    )
    def revoke_key(project: str, key_id: str, request: Request) -> ApiKeyOut:
        return ApiKeyOut.from_domain(svc(request).revoke_key(project, key_id), now(request))

    return router
