from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from controlplane.api.auth import request_principal
from controlplane.api.errors import PlatformRoute
from controlplane.application.identity import role_in, visible_project_ids
from controlplane.application.jobs import resolve_project
from controlplane.application.ports import UnitOfWork
from controlplane.application.schedule_calendar import preview
from controlplane.domain.access import ProjectRole
from controlplane.domain.schedules import (
    ConcurrencyPolicy,
    ConcurrencyScope,
    ExecutionStatus,
    MissedRunPolicy,
    Schedule,
    ScheduleExecution,
    TargetKind,
    VersionPolicy,
)


class ScheduleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=3, max_length=40)
    target_kind: TargetKind
    target_name: str = Field(min_length=3, max_length=40)
    cron: str = Field(max_length=128)
    timezone: str = Field(default="UTC", max_length=64)
    version_policy: VersionPolicy = VersionPolicy.PINNED
    version: int | None = Field(default=None, ge=1)
    concurrency_policy: ConcurrencyPolicy = ConcurrencyPolicy.FORBID
    concurrency_scope: ConcurrencyScope = ConcurrencyScope.TARGET
    missed_run_policy: MissedRunPolicy = MissedRunPolicy.SKIP
    deadline_seconds: int = Field(default=300, ge=1, le=604800)
    queue_ttl_seconds: int = Field(default=86400, ge=1, le=604800)
    max_queue_size: int = Field(default=100, ge=1, le=1000)
    timeout_seconds: int = Field(default=3600, ge=1, le=604800)
    parameters: dict[str, Any] = Field(default_factory=dict)
    parameter_bindings: dict[str, str] = Field(default_factory=dict)
    paused: bool = False


class ScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    cron: str | None = Field(default=None, max_length=128)
    timezone: str | None = Field(default=None, max_length=64)
    version_policy: VersionPolicy | None = None
    version: int | None = Field(default=None, ge=1)
    concurrency_policy: ConcurrencyPolicy | None = None
    concurrency_scope: ConcurrencyScope | None = None
    missed_run_policy: MissedRunPolicy | None = None
    deadline_seconds: int | None = Field(default=None, ge=1, le=604800)
    queue_ttl_seconds: int | None = Field(default=None, ge=1, le=604800)
    max_queue_size: int | None = Field(default=None, ge=1, le=1000)
    timeout_seconds: int | None = Field(default=None, ge=1, le=604800)
    parameters: dict[str, Any] | None = None
    parameter_bindings: dict[str, str] | None = None
    paused: bool | None = None


class ExecutionOut(BaseModel):
    parameters: dict[str, Any] = Field(default_factory=dict)
    id: UUID
    schedule_id: UUID
    project_id: UUID
    scheduled_for_utc: datetime
    schedule_revision: int
    resolved_definition_id: UUID | None
    target_kind: TargetKind
    target_name: str
    status: ExecutionStatus
    reason: str | None
    pipeline_run_id: UUID | None
    job_run_id: UUID | None
    run_status: str | None = None
    expires_at: datetime
    created_at: datetime
    updated_at: datetime


class ScheduleOut(ScheduleCreate):
    id: UUID
    project_id: UUID
    project_name: str
    revision: int
    next_run_at: datetime
    next_executions: list[datetime]
    last_execution: ExecutionOut | None
    created_at: datetime
    updated_at: datetime


class ScheduleList(BaseModel):
    items: list[ScheduleOut]
    limit: int
    offset: int


class ExecutionList(BaseModel):
    items: list[ExecutionOut]
    limit: int
    offset: int


class SchedulePreview(BaseModel):
    cron: str = Field(max_length=128)
    timezone: str = Field(default="UTC", max_length=64)


class PreviewOut(BaseModel):
    executions: list[datetime]


def execution_out(uow: UnitOfWork, entity: ScheduleExecution) -> ExecutionOut:
    run = (
        uow.pipeline_runs.get(entity.pipeline_run_id)
        if entity.pipeline_run_id
        else (uow.runs.get(entity.job_run_id) if entity.job_run_id else None)
    )
    return ExecutionOut(**asdict(entity), run_status=run.status.value if run else None)


def schedule_out(request: Request, entity: Schedule) -> ScheduleOut:
    with request.app.state.uow_factory() as uow:
        project = uow.projects.get(entity.project_id)
        assert project is not None
        history = uow.schedules.history(entity.id, 1, 0)
        return ScheduleOut(
            **asdict(entity),
            project_name=project.name,
            next_executions=[
                entity.next_run_at,
                *preview(entity.cron, entity.timezone, entity.next_run_at, 4),
            ],
            last_execution=execution_out(uow, history[0]) if history else None,
        )


def schedules_router() -> APIRouter:
    router = APIRouter(route_class=PlatformRoute, tags=["schedules"])

    @router.post(
        "/projects/{project}/schedules",
        response_model=ScheduleOut,
        status_code=status.HTTP_201_CREATED,
    )
    def create(project: str, body: ScheduleCreate, request: Request) -> ScheduleOut:
        return schedule_out(
            request, request.app.state.schedules.create(project, **body.model_dump())
        )

    @router.get("/schedules", response_model=ScheduleList)
    def list_all(
        request: Request,
        project: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        target_kind: TargetKind | None = None,
        target_name: str | None = None,
        paused: bool | None = None,
    ) -> ScheduleList:
        with request.app.state.uow_factory() as uow:
            principal = request_principal(request)
            visible = visible_project_ids(uow, principal)
            if visible is not None:
                visible = [
                    id
                    for id in visible
                    if (role := role_in(uow, id, principal)) is not None
                    and role.includes(ProjectRole.VIEWER)
                ]
            if project:
                id = resolve_project(uow, project).id
                visible = [id] if visible is None or id in visible else []
        return ScheduleList(
            items=[
                schedule_out(request, s)
                for s in request.app.state.schedules.list(
                    visible,
                    limit,
                    offset,
                    target_kind=target_kind,
                    target_name=target_name,
                    paused=paused,
                )
            ],
            limit=limit,
            offset=offset,
        )

    @router.get("/projects/{project}/schedules", response_model=ScheduleList)
    def list_project(
        project: str,
        request: Request,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        target_kind: TargetKind | None = None,
        target_name: str | None = None,
        paused: bool | None = None,
    ) -> ScheduleList:
        return list_all(request, project, limit, offset, target_kind, target_name, paused)

    @router.get("/schedules/{schedule_id}", response_model=ScheduleOut)
    def get(schedule_id: UUID, request: Request) -> ScheduleOut:
        return schedule_out(request, request.app.state.schedules.get(schedule_id))

    @router.patch("/schedules/{schedule_id}", response_model=ScheduleOut)
    def update(schedule_id: UUID, body: ScheduleUpdate, request: Request) -> ScheduleOut:
        changes = body.model_dump(exclude_unset=True, exclude={"expected_revision"})
        if any(value is None and key != "version" for key, value in changes.items()):
            from controlplane.domain.errors import InvalidArgument

            raise InvalidArgument("only version may be null")
        return schedule_out(
            request,
            request.app.state.schedules.update(schedule_id, body.expected_revision, changes),
        )

    @router.get("/schedules/{schedule_id}/executions", response_model=ExecutionList)
    def history(
        schedule_id: UUID,
        request: Request,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ExecutionList:
        entities = request.app.state.schedules.history(schedule_id, limit, offset)
        with request.app.state.uow_factory() as uow:
            return ExecutionList(
                items=[execution_out(uow, e) for e in entities], limit=limit, offset=offset
            )

    @router.post("/schedule-preview", response_model=PreviewOut)
    def preview_cron(body: SchedulePreview, request: Request) -> PreviewOut:
        return PreviewOut(executions=preview(body.cron, body.timezone, request.app.state.clock()))

    @router.get("/runs/{run_id}/schedule", response_model=ExecutionOut | None)
    def job_origin(run_id: UUID, request: Request) -> ExecutionOut | None:
        with request.app.state.uow_factory() as uow:
            entity = uow.schedules.for_run(run_id, TargetKind.JOB)
            return execution_out(uow, entity) if entity else None

    @router.get("/pipeline-runs/{run_id}/schedule", response_model=ExecutionOut | None)
    def pipeline_origin(run_id: UUID, request: Request) -> ExecutionOut | None:
        with request.app.state.uow_factory() as uow:
            entity = uow.schedules.for_run(run_id, TargetKind.PIPELINE)
            return execution_out(uow, entity) if entity else None

    return router
