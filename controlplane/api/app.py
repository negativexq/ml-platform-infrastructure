from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, FastAPI, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from controlplane.api.deployments import deployments_router
from controlplane.api.jobs_runs import jobs_router, runs_router
from controlplane.api.models import model_versions_router, models_router
from controlplane.api.overview import overview_router
from controlplane.api.pipelines import pipeline_runs_router, pipelines_router
from controlplane.api.rollouts import rollouts_router
from controlplane.api.schemas import ErrorOut, ProjectCreate, ProjectList, ProjectOut
from controlplane.application.deployments import DeploymentService
from controlplane.application.jobs import JobService
from controlplane.application.models import EvaluationService, ModelService, PromotionService
from controlplane.application.overview import OverviewService
from controlplane.application.pipeline_runs import PipelineRunService
from controlplane.application.pipelines import PipelineService
from controlplane.application.projects import (
    Clock,
    CreateProject,
    ProjectService,
    UnitOfWorkFactory,
    utc_now,
)
from controlplane.application.providers import (
    ExperimentProvider,
    MetricsProvider,
    ServingProvider,
    WorkflowProvider,
)
from controlplane.application.rollouts import RolloutService
from controlplane.application.runs import RunService
from controlplane.domain.errors import (
    AlreadyExists,
    Conflict,
    DomainError,
    IllegalTransition,
    InvalidArgument,
    NotFound,
)
from controlplane.ui import CONTENT_SECURITY_POLICY, STATIC_DIR

_STATUS_FOR: dict[type[DomainError], tuple[int, str]] = {
    NotFound: (status.HTTP_404_NOT_FOUND, "not_found"),
    AlreadyExists: (status.HTTP_409_CONFLICT, "already_exists"),
    Conflict: (status.HTTP_409_CONFLICT, "conflict"),
    IllegalTransition: (status.HTTP_409_CONFLICT, "illegal_transition"),
    InvalidArgument: (status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_argument"),
}


def _domain_error_handler(_: Request, exc: Exception) -> JSONResponse:
    code, label = next(
        (v for k, v in _STATUS_FOR.items() if isinstance(exc, k)),
        (status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error"),
    )
    return JSONResponse(status_code=code, content={"error": {"code": label, "message": str(exc)}})


def _projects_router() -> APIRouter:
    router = APIRouter(prefix="/projects", tags=["projects"])
    errors: dict[int | str, dict[str, Any]] = {
        404: {"model": ErrorOut},
        409: {"model": ErrorOut},
        422: {"model": ErrorOut},
    }

    def service(request: Request) -> ProjectService:
        svc: ProjectService = request.app.state.projects
        return svc

    @router.post(
        "",
        response_model=ProjectOut,
        status_code=status.HTTP_201_CREATED,
        responses={
            200: {"model": ProjectOut, "description": "Identical project already exists"},
            **errors,
        },
        summary="Create a project (idempotent)",
    )
    def create_project(body: ProjectCreate, request: Request, response: Response) -> ProjectOut:
        project, created = service(request).create(
            CreateProject(
                name=body.name, display_name=body.display_name, description=body.description
            )
        )
        if not created:
            response.status_code = status.HTTP_200_OK
        return ProjectOut.from_domain(project)

    @router.get("", response_model=ProjectList, summary="List projects")
    def list_projects(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ProjectList:
        items = service(request).list(limit=limit, offset=offset)
        return ProjectList(
            items=[ProjectOut.from_domain(p) for p in items], limit=limit, offset=offset
        )

    @router.get(
        "/{project_id}", response_model=ProjectOut, responses=errors, summary="Get a project"
    )
    def get_project(project_id: UUID, request: Request) -> ProjectOut:
        return ProjectOut.from_domain(service(request).get(project_id))

    @router.delete(
        "/{project_id}",
        response_model=ProjectOut,
        status_code=status.HTTP_202_ACCEPTED,
        responses=errors,
        summary="Request project deletion (idempotent; the reconciler cleans up)",
    )
    def delete_project(project_id: UUID, request: Request) -> ProjectOut:
        return ProjectOut.from_domain(service(request).request_delete(project_id))

    return router


def create_app(
    uow_factory: UnitOfWorkFactory,
    clock: Clock = utc_now,
    workflow: WorkflowProvider | None = None,
    experiments: ExperimentProvider | None = None,
    serving: ServingProvider | None = None,
    metrics: MetricsProvider | None = None,
    ui: bool = True,
) -> FastAPI:
    app = FastAPI(
        title="ML Platform Control Plane",
        version="0.1.0",
        description="Platform API. PostgreSQL owns lifecycle state; MLflow, Argo and "
        "KServe are adapters behind it.",
    )
    app.state.projects = ProjectService(uow_factory, clock)
    app.state.jobs = JobService(uow_factory, clock)
    app.state.runs = RunService(uow_factory, clock)
    app.state.pipelines = PipelineService(uow_factory, clock)
    app.state.pipeline_runs = PipelineRunService(uow_factory, clock, experiments)
    app.state.models = ModelService(uow_factory, clock, experiments)
    app.state.evaluations = (
        EvaluationService(uow_factory, experiments, clock) if experiments is not None else None
    )
    app.state.promotions = PromotionService(uow_factory, clock)
    app.state.deployments = DeploymentService(uow_factory, clock, experiments, serving)
    app.state.rollouts = RolloutService(uow_factory, app.state.deployments, clock)
    app.state.overview = OverviewService(uow_factory, serving, metrics)
    app.state.workflow = workflow
    app.state.experiments = experiments
    app.add_exception_handler(DomainError, _domain_error_handler)
    app.include_router(_projects_router())
    app.include_router(jobs_router())
    app.include_router(runs_router())
    app.include_router(pipelines_router())
    app.include_router(pipeline_runs_router())
    app.include_router(models_router())
    app.include_router(model_versions_router())
    app.include_router(deployments_router())
    app.include_router(rollouts_router())
    app.include_router(overview_router())

    if ui:
        _mount_ui(app)

    @app.get("/healthz", tags=["ops"], summary="Liveness")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _mount_ui(app: FastAPI) -> None:
    app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")

    @app.middleware("http")
    async def ui_security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        if request.url.path.startswith("/ui"):
            response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/ui/")
