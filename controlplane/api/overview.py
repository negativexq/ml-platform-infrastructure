from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

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


class AuditEventOut(BaseModel):
    id: UUID
    occurred_at: datetime
    actor: str
    action: str
    entity_type: str
    entity_id: UUID
    payload: dict[str, Any]


class AuditOut(BaseModel):
    items: list[AuditEventOut]


def overview_router() -> APIRouter:
    router = APIRouter(prefix="/projects/{project}", tags=["overview"])

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
                )
                for e in events
            ]
        )

    return router
