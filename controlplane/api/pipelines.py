from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse

from controlplane.api.errors import PlatformRoute
from controlplane.api.schemas import ErrorOut
from controlplane.api.schemas_pipelines import (
    PipelineCreate,
    PipelineList,
    PipelineOut,
    PipelineRunCreate,
    PipelineRunList,
    PipelineRunOut,
    PipelineRunSummary,
    TrackedRunOut,
    TrackingOut,
)
from controlplane.application.pipeline_runs import PipelineRunService
from controlplane.application.pipelines import CreatePipeline, PipelineService, StepInput
from controlplane.application.providers import WorkflowProvider
from controlplane.domain.states import RunStatus

_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorOut},
    409: {"model": ErrorOut},
    422: {"model": ErrorOut},
}
IdempotencyKey = Annotated[
    str | None,
    Header(
        alias="Idempotency-Key",
        max_length=200,
        description="Repeating a request with the same key never creates a second run.",
    ),
]


def pipelines_router() -> APIRouter:
    router = APIRouter(route_class=PlatformRoute, prefix="/projects/{project}", tags=["pipelines"])

    def defs(request: Request) -> PipelineService:
        service: PipelineService = request.app.state.pipelines
        return service

    def runs(request: Request) -> PipelineRunService:
        service: PipelineRunService = request.app.state.pipeline_runs
        return service

    @router.post(
        "/pipelines",
        response_model=PipelineOut,
        status_code=status.HTTP_201_CREATED,
        responses={
            200: {"model": PipelineOut, "description": "Identical to the latest version"},
            **_ERRORS,
        },
        summary="Create a pipeline version (cycles and unknown dependencies are rejected)",
    )
    def create_pipeline(
        project: str, body: PipelineCreate, request: Request, response: Response
    ) -> PipelineOut:
        definition, created = defs(request).create(
            project,
            CreatePipeline(
                name=body.name,
                steps=[StepInput(s.name, s.job, tuple(s.depends_on)) for s in body.steps],
            ),
        )
        if not created:
            response.status_code = status.HTTP_200_OK
        return PipelineOut.from_domain(definition)

    @router.get(
        "/pipelines",
        response_model=PipelineList,
        responses=_ERRORS,
        summary="Latest version of every pipeline",
    )
    def list_pipelines(project: str, request: Request) -> PipelineList:
        return PipelineList(items=[PipelineOut.from_domain(d) for d in defs(request).list(project)])

    @router.get(
        "/pipelines/{name}",
        response_model=PipelineOut,
        responses=_ERRORS,
        summary="Get a pipeline version",
    )
    def get_pipeline(
        project: str,
        name: str,
        request: Request,
        version: Annotated[int | None, Query(ge=1)] = None,
    ) -> PipelineOut:
        return PipelineOut.from_domain(defs(request).get(project, name, version))

    @router.post(
        "/pipelines/{name}/runs",
        response_model=PipelineRunOut,
        status_code=status.HTTP_202_ACCEPTED,
        responses={
            200: {"model": PipelineRunOut, "description": "Replay of an earlier request"},
            **_ERRORS,
        },
        summary="Run a pipeline (asynchronous; poll GET /pipeline-runs/{id})",
        tags=["pipeline runs"],
    )
    def start_run(
        project: str,
        name: str,
        request: Request,
        response: Response,
        body: PipelineRunCreate | None = None,
        version: Annotated[int | None, Query(ge=1)] = None,
        idempotency_key: IdempotencyKey = None,
    ) -> PipelineRunOut:
        view, created = runs(request).create(
            project,
            name,
            version=version,
            commit_sha=body.commit_sha if body else None,
            idempotency_key=idempotency_key,
        )
        if not created:
            response.status_code = status.HTTP_200_OK
        return PipelineRunOut.from_view(view)

    @router.get(
        "/pipeline-runs",
        response_model=PipelineRunList,
        responses=_ERRORS,
        summary="List pipeline runs, newest first",
        tags=["pipeline runs"],
    )
    def list_runs(
        project: str,
        request: Request,
        pipeline: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        status: Annotated[list[RunStatus] | None, Query(description="Repeat to match any")] = None,
    ) -> PipelineRunList:
        service = runs(request)
        items = service.list(
            project, pipeline_name=pipeline, limit=limit, offset=offset, statuses=status
        )
        labels = service.definition_labels(items)
        return PipelineRunList(
            items=[
                PipelineRunSummary.from_domain(r, labels.get(r.pipeline_definition_id))
                for r in items
            ],
            limit=limit,
            offset=offset,
        )

    return router


def pipeline_runs_router() -> APIRouter:
    router = APIRouter(route_class=PlatformRoute, prefix="/pipeline-runs", tags=["pipeline runs"])

    def svc(request: Request) -> PipelineRunService:
        service: PipelineRunService = request.app.state.pipeline_runs
        return service

    @router.get(
        "/{run_id}",
        response_model=PipelineRunOut,
        responses=_ERRORS,
        summary="Run with per-step status",
    )
    def get_run(run_id: UUID, request: Request) -> PipelineRunOut:
        return PipelineRunOut.from_view(svc(request).view(run_id))

    @router.post(
        "/{run_id}/cancel",
        response_model=PipelineRunOut,
        status_code=status.HTTP_202_ACCEPTED,
        responses=_ERRORS,
        summary="Request cancellation (idempotent)",
    )
    def cancel(run_id: UUID, request: Request) -> PipelineRunOut:
        return PipelineRunOut.from_view(svc(request).request_cancel(run_id))

    @router.get(
        "/{run_id}/tracking",
        response_model=TrackingOut,
        responses={503: {"model": ErrorOut}, **_ERRORS},
        summary="Tracked experiment runs (params, metrics, artifacts) for this run, by platform id",
    )
    def tracking(run_id: UUID, request: Request) -> TrackingOut:
        service = svc(request)
        if request.app.state.experiments is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "no tracking provider configured"
            )
        return TrackingOut(
            pipeline_run_id=run_id,
            runs=[TrackedRunOut.from_domain(t) for t in service.tracking(run_id)],
        )

    @router.get(
        "/{run_id}/steps/{step}/logs",
        response_class=PlainTextResponse,
        responses={503: {"model": ErrorOut}, **_ERRORS},
        summary="Step logs, read through the platform",
    )
    def step_logs(run_id: UUID, step: str, request: Request) -> str:
        workflow: WorkflowProvider | None = request.app.state.workflow
        if workflow is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "no workflow provider configured"
            )
        view = svc(request).view(run_id)
        if step not in {s.step_name for s in view.steps}:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"step {step!r} not in this run")
        if view.run.external_ref is None:
            return ""
        return workflow.get_logs(view.run.external_ref, step)

    return router
