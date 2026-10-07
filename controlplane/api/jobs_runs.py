from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse

from controlplane.api.errors import PlatformRoute
from controlplane.api.log_stream import LogStreamTicket, ticket
from controlplane.api.schemas import ErrorOut
from controlplane.api.schemas_runs import JobCreate, JobList, JobOut, RunCreate, RunList, RunOut
from controlplane.application.jobs import CreateJob, JobService
from controlplane.application.providers import WorkflowProvider
from controlplane.application.runs import RunService
from controlplane.application.workflow_compiler import MAIN_STEP
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
        description="Repeating a request with the same key never creates a second workload.",
    ),
]


def jobs_router() -> APIRouter:
    router = APIRouter(route_class=PlatformRoute, prefix="/projects/{project}/jobs", tags=["jobs"])

    def svc(request: Request) -> JobService:
        service: JobService = request.app.state.jobs
        return service

    @router.post(
        "",
        response_model=JobOut,
        status_code=status.HTTP_201_CREATED,
        responses={
            200: {"model": JobOut, "description": "Identical job already exists"},
            **_ERRORS,
        },
        summary="Create a job definition (idempotent; definitions are immutable)",
    )
    def create_job(project: str, body: JobCreate, request: Request, response: Response) -> JobOut:
        job, created = svc(request).create(
            project,
            CreateJob(
                name=body.name,
                parameter_schema=body.parameter_schema,
                image=body.image,
                command=tuple(body.command),
                resources=body.resources,
                env=body.env,
                timeout_seconds=body.timeout_seconds,
                secret_refs=body.secret_refs.to_domain(),
            ),
        )
        if not created:
            response.status_code = status.HTTP_200_OK
        return JobOut.from_domain(job)

    @router.get("", response_model=JobList, responses=_ERRORS, summary="List job definitions")
    def list_jobs(project: str, request: Request) -> JobList:
        return JobList(items=[JobOut.from_domain(j) for j in svc(request).list(project)])

    @router.get("/{job}", response_model=JobOut, responses=_ERRORS, summary="Get a job definition")
    def get_job(project: str, job: str, request: Request) -> JobOut:
        return JobOut.from_domain(svc(request).get(project, job))

    @router.post(
        "/{job}/runs",
        response_model=RunOut,
        status_code=status.HTTP_202_ACCEPTED,
        responses={
            200: {"model": RunOut, "description": "Replay of an earlier request"},
            **_ERRORS,
        },
        summary="Start a run (asynchronous; poll GET /runs/{id})",
        tags=["runs"],
    )
    def start_run(
        project: str,
        job: str,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey = None,
        body: RunCreate | None = None,
    ) -> RunOut:
        runs: RunService = request.app.state.runs
        run, created = runs.create(
            project,
            job,
            idempotency_key=idempotency_key,
            timeout_seconds=body.timeout_seconds if body else None,
            parameters=body.parameters if body else None,
        )
        if not created:
            response.status_code = status.HTTP_200_OK
        return RunOut.from_domain(run)

    return router


def runs_router() -> APIRouter:
    router = APIRouter(route_class=PlatformRoute, tags=["runs"])

    def svc(request: Request) -> RunService:
        service: RunService = request.app.state.runs
        return service

    @router.get(
        "/projects/{project}/runs",
        response_model=RunList,
        responses=_ERRORS,
        summary="List runs, newest first",
    )
    def list_runs(
        project: str,
        request: Request,
        job: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        status: Annotated[list[RunStatus] | None, Query(description="Repeat to match any")] = None,
    ) -> RunList:
        service = svc(request)
        items = service.list(project, job_name=job, limit=limit, offset=offset, statuses=status)
        names = service.job_names(items[0].project_id) if items else {}
        return RunList(
            items=[RunOut.from_domain(r, names.get(r.job_definition_id)) for r in items],
            limit=limit,
            offset=offset,
        )

    @router.get("/runs/{run_id}", response_model=RunOut, responses=_ERRORS, summary="Get a run")
    def get_run(run_id: UUID, request: Request) -> RunOut:
        service = svc(request)
        run = service.get(run_id)
        return RunOut.from_domain(run, service.job_names(run.project_id).get(run.job_definition_id))

    @router.post(
        "/runs/{run_id}/cancel",
        response_model=RunOut,
        status_code=status.HTTP_202_ACCEPTED,
        responses=_ERRORS,
        summary="Request cancellation (idempotent)",
    )
    def cancel_run(run_id: UUID, request: Request) -> RunOut:
        return RunOut.from_domain(svc(request).request_cancel(run_id))

    @router.post(
        "/runs/{run_id}/retry",
        response_model=RunOut,
        status_code=status.HTTP_202_ACCEPTED,
        responses=_ERRORS,
        summary="Retry a finished run as a new run; the original is left untouched",
    )
    def retry_run(run_id: UUID, request: Request, idempotency_key: IdempotencyKey = None) -> RunOut:
        run, _ = svc(request).retry(run_id, idempotency_key=idempotency_key)
        return RunOut.from_domain(run)

    @router.get(
        "/runs/{run_id}/logs",
        response_class=PlainTextResponse,
        responses={410: {"model": ErrorOut}, 503: {"model": ErrorOut}, **_ERRORS},
        summary="Run logs, read through the platform",
    )
    def run_logs(run_id: UUID, request: Request) -> str:
        workflow: WorkflowProvider | None = request.app.state.workflow
        if workflow is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "no workflow provider configured"
            )
        run = svc(request).get(run_id)
        if run.external_ref is None:
            return ""  # not submitted yet: nothing has run
        if run.workflow_cleaned_at is not None:
            raise HTTPException(410, "workflow logs expired under the retention policy")
        return workflow.get_logs(run.external_ref, MAIN_STEP)

    @router.post("/runs/{run_id}/logs/stream-ticket", response_model=LogStreamTicket)
    def run_log_ticket(run_id: UUID, request: Request, response: Response) -> LogStreamTicket:
        run = svc(request).get(run_id)
        if run.workflow_cleaned_at is not None:
            raise HTTPException(410, "workflow logs expired under the retention policy")
        return ticket(request, response, run.external_ref, MAIN_STEP)

    return router
