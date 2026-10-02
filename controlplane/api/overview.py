from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from controlplane.api.errors import PlatformRoute
from controlplane.api.schemas import ErrorOut
from controlplane.application.overview import OverviewService
from controlplane.domain.states import DeploymentStatus, EndpointStatus

_ERRORS: dict[int | str, dict[str, Any]] = {404: {"model": ErrorOut}}


class SummaryOut(BaseModel):
    project_id: UUID
    jobs: int
    pipelines: int
    runs: int
    pipeline_runs: int
    models: int
    champions: int
    deployments: int
    deployments_ready: int
    endpoints: int
    active_rollouts: int


class EndpointItemOut(BaseModel):
    id: UUID
    name: str
    status: EndpointStatus
    url: str | None
    deployment_status: DeploymentStatus
    active_revision: int | None


class EndpointListOut(BaseModel):
    items: list[EndpointItemOut]


class RevisionMetricsOut(BaseModel):
    revision: int
    model: str
    model_version: int
    traffic_percent: int
    p95_latency_ms: float | None
    error_rate: float | None
    requests_per_second: float | None
    requests: float | None


class EndpointMetricsOut(BaseModel):
    endpoint: str
    available: bool
    error: str | None
    revisions: list[RevisionMetricsOut]


class MetricsPointOut(BaseModel):
    at: datetime
    p95_latency_ms: float | None
    error_rate: float | None
    requests_per_second: float | None


class RevisionHistoryOut(BaseModel):
    revision: int
    model: str
    model_version: int
    role: str = Field(description="stable, canary, or serving (no rollout)")
    points: list[MetricsPointOut]


class HistoryMarkerOut(BaseModel):
    at: datetime
    label: str


class EndpointHistoryOut(BaseModel):
    endpoint: str
    available: bool
    error: str | None
    start: datetime
    end: datetime
    step_seconds: int
    revisions: list[RevisionHistoryOut]
    max_error_rate: float | None = Field(description="the live canary's gate, if any")
    max_p95_latency_ms: float | None
    markers: list[HistoryMarkerOut]


class AuditEventOut(BaseModel):
    id: UUID
    occurred_at: datetime
    actor: str
    action: str
    entity_type: str
    entity_id: UUID
    payload: dict[str, Any]
    trace_id: str | None = None


class AuditOut(BaseModel):
    items: list[AuditEventOut]


def overview_router() -> APIRouter:
    router = APIRouter(route_class=PlatformRoute, prefix="/projects/{project}", tags=["overview"])

    def svc(request: Request) -> OverviewService:
        service: OverviewService = request.app.state.overview
        return service

    @router.get(
        "/summary", response_model=SummaryOut, responses=_ERRORS, summary="Counts for a project"
    )
    def summary(project: str, request: Request) -> SummaryOut:
        s = svc(request).summary(project)
        return SummaryOut(
            project_id=s.project_id,
            jobs=s.jobs,
            pipelines=s.pipelines,
            runs=s.runs,
            pipeline_runs=s.pipeline_runs,
            models=s.models,
            champions=s.champions,
            deployments=s.deployments,
            deployments_ready=s.deployments_ready,
            endpoints=s.endpoints,
            active_rollouts=s.active_rollouts,
        )

    @router.get(
        "/endpoints", response_model=EndpointListOut, responses=_ERRORS, summary="List endpoints"
    )
    def list_endpoints(project: str, request: Request) -> EndpointListOut:
        return EndpointListOut(
            items=[
                EndpointItemOut(
                    id=i.endpoint.id,
                    name=i.endpoint.name,
                    status=i.endpoint.status,
                    url=i.endpoint.url,
                    deployment_status=i.deployment_status,
                    active_revision=i.active_revision,
                )
                for i in svc(request).endpoints(project)
            ]
        )

    @router.get(
        "/endpoints/{name}/metrics",
        response_model=EndpointMetricsOut,
        responses=_ERRORS,
        summary="p95 / error rate / RPS per revision that receives traffic",
    )
    def endpoint_metrics(project: str, name: str, request: Request) -> EndpointMetricsOut:
        v = svc(request).endpoint_metrics(project, name)
        return EndpointMetricsOut(
            endpoint=v.endpoint,
            available=v.available,
            error=v.error,
            revisions=[
                RevisionMetricsOut(
                    revision=r.revision,
                    model=r.model,
                    model_version=r.model_version,
                    traffic_percent=r.traffic_percent,
                    p95_latency_ms=r.p95_latency_ms,
                    error_rate=r.error_rate,
                    requests_per_second=r.requests_per_second,
                    requests=r.requests,
                )
                for r in v.revisions
            ],
        )

    @router.get(
        "/endpoints/{name}/metrics/history",
        response_model=EndpointHistoryOut,
        responses=_ERRORS,
        summary="p95 / error rate / RPS over time, per revision serving now",
    )
    def endpoint_history(
        project: str,
        name: str,
        request: Request,
        minutes: Annotated[int, Query(ge=5, le=1440)] = 60,
    ) -> EndpointHistoryOut:
        v = svc(request).endpoint_history(project, name, minutes=minutes)
        return EndpointHistoryOut(
            endpoint=v.endpoint,
            available=v.available,
            error=v.error,
            start=v.start,
            end=v.end,
            step_seconds=v.step_seconds,
            revisions=[
                RevisionHistoryOut(
                    revision=r.revision,
                    model=r.model,
                    model_version=r.model_version,
                    role=r.role,
                    points=[
                        MetricsPointOut(
                            at=p.at,
                            p95_latency_ms=p.p95_latency_ms,
                            error_rate=p.error_rate,
                            requests_per_second=p.requests_per_second,
                        )
                        for p in r.points
                    ],
                )
                for r in v.revisions
            ],
            max_error_rate=v.max_error_rate,
            max_p95_latency_ms=v.max_p95_latency_ms,
            markers=[HistoryMarkerOut(at=m.at, label=m.label) for m in v.markers],
        )

    @router.get(
        "/audit", response_model=AuditOut, responses=_ERRORS, summary="Audit trail, newest first"
    )
    def audit(
        project: str,
        request: Request,
        entity_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> AuditOut:
        events = svc(request).audit(project, entity_id=entity_id, limit=limit)
        return AuditOut(
            items=[
                AuditEventOut(
                    id=e.id,
                    occurred_at=e.occurred_at,
                    actor=e.actor,
                    action=e.action,
                    entity_type=e.entity_type,
                    entity_id=e.entity_id,
                    payload=dict(e.payload),
                    trace_id=e.trace_id,
                )
                for e in events
            ]
        )

    return router
