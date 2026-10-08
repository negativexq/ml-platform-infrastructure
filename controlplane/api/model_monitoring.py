from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from controlplane.api.errors import PlatformRoute
from controlplane.api.schemas_monitoring import MonitoringResult
from controlplane.api.schemas_runs import JobList, JobOut
from controlplane.domain.errors import NotFound


class MonitoringCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(max_length=40)
    model_version_id: UUID
    reference_dataset_id: UUID
    observed_dataset_id: UUID
    features: list[str] = Field(min_length=1, max_length=32)
    feedback_dataset_id: UUID | None = None
    task: Literal["REGRESSION", "CLASSIFICATION"] = "REGRESSION"
    entity_key: str = Field(default="", max_length=128)
    prediction_column: str = Field(default="prediction", max_length=128)
    label_column: str = Field(default="actual", max_length=128)
    psi_threshold: float = Field(default=0.2, gt=0, le=100)
    missing_rate_threshold: float = Field(default=0.1, gt=0, le=1)
    minimum_rows: int = Field(default=100, ge=1, le=100000000)
    batch_size: int = Field(default=1000, ge=1, le=100000)
    max_rows: int = Field(default=10000000, ge=1, le=100000000)
    max_bytes: int = Field(default=1073741824, ge=1, le=2147483648)
    max_join_bytes: int = Field(default=1073741824, ge=1, le=2147483648)
    timeout_seconds: int = Field(default=3600, ge=1, le=604800)
    resources: dict[str, str] = Field(default_factory=lambda: {"cpu": "1", "memory": "2Gi"})


class MonitoringReportOut(BaseModel):
    model_name: str
    model_version: int
    check_name: str
    id: UUID
    project_id: UUID
    job_definition_id: UUID
    model_version_id: UUID
    reference_dataset_id: UUID
    observed_dataset_id: UUID
    feedback_dataset_id: UUID | None
    job_run_id: UUID | None
    pipeline_run_id: UUID | None
    step: str
    status: Literal["STABLE", "DRIFTED", "INSUFFICIENT_DATA"]
    result: MonitoringResult
    created_at: datetime


class MonitoringReportList(BaseModel):
    items: list[MonitoringReportOut]
    limit: int
    offset: int


def monitoring_router() -> APIRouter:
    router = APIRouter(
        route_class=PlatformRoute,
        prefix="/projects/{project}/model-monitoring",
        tags=["model monitoring"],
    )

    @router.post("/checks", response_model=JobOut, status_code=201)
    def create(
        project: str, body: MonitoringCreate, request: Request, response: Response
    ) -> JobOut:
        job, created = request.app.state.model_monitoring.create(project, **body.model_dump())
        if not created:
            response.status_code = 200
        return JobOut.from_domain(job)

    @router.get("/checks", response_model=JobList)
    def checks(project: str, request: Request) -> JobList:
        return JobList(
            items=[
                JobOut.from_domain(job)
                for job in request.app.state.jobs.list(project)
                if job.monitoring_spec
            ]
        )

    @router.get("/checks/{name}", response_model=JobOut)
    def check(project: str, name: str, request: Request) -> JobOut:
        job = request.app.state.jobs.get(project, name)
        if not job.monitoring_spec:
            raise NotFound("monitoring check", name)
        return JobOut.from_domain(job)

    @router.get("/reports", response_model=MonitoringReportList)
    def reports(
        project: str,
        request: Request,
        model_version_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
        job_run_id: UUID | None = None,
        pipeline_run_id: UUID | None = None,
    ) -> MonitoringReportList:
        return MonitoringReportList(
            items=[
                MonitoringReportOut(**asdict(report))
                for report in request.app.state.model_monitoring.reports(
                    project, model_version_id, limit, offset, job_run_id, pipeline_run_id
                )
            ],
            limit=limit,
            offset=offset,
        )

    @router.get("/reports/{id}", response_model=MonitoringReportOut)
    def report(project: str, id: UUID, request: Request) -> MonitoringReportOut:
        return MonitoringReportOut(**asdict(request.app.state.model_monitoring.report(project, id)))

    return router
