from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.telemetry import TelemetryConfig

from controlplane.api.api_access import api_access_router
from controlplane.api.auth import AuthConfig, AuthMiddleware, authorize, request_principal
from controlplane.api.deployments import deployments_router
from controlplane.api.errors import DomainHttpError, PlatformRoute, handle_domain_error
from controlplane.api.identity import identity_router, login_router
from controlplane.api.jobs_runs import jobs_router, runs_router
from controlplane.api.models import model_versions_router, models_router
from controlplane.api.notifications import notifications_router
from controlplane.api.overview import overview_router
from controlplane.api.pipelines import pipeline_runs_router, pipelines_router
from controlplane.api.platform import platform_router
from controlplane.api.rollouts import rollouts_router
from controlplane.api.schedules import schedules_router
from controlplane.api.schemas import (
    ErrorOut,
    GpuQuotaIn,
    ProjectCreate,
    ProjectList,
    ProjectOut,
)
from controlplane.api.secrets import secrets_router
from controlplane.application.api_access import ApiAccessService
from controlplane.application.deployments import DeploymentService
from controlplane.application.identity import visible_project_ids
from controlplane.application.jobs import JobService
from controlplane.application.members import MembershipService
from controlplane.application.models import EvaluationService, ModelService, PromotionService
from controlplane.application.notifications import NotificationService
from controlplane.application.overview import OverviewService
from controlplane.application.pipeline_runs import PipelineRunService
from controlplane.application.pipelines import PipelineService
from controlplane.application.platform import PlatformService
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
    PlatformTelemetry,
    ServingProvider,
    UsageProvider,
    WorkflowProvider,
)
from controlplane.application.rollouts import RolloutService
from controlplane.application.runs import RunService
from controlplane.application.schedules import ScheduleService
from controlplane.application.secrets import ProjectSecretService, SecretProvider
from controlplane.domain.errors import (
    DomainError,
)
from controlplane.health import add_readiness
from controlplane.http_telemetry import telemetry_config
from controlplane.ui import CONTENT_SECURITY_POLICY, STATIC_DIR


def _projects_router() -> APIRouter:
    router = APIRouter(route_class=PlatformRoute, prefix="/projects", tags=["projects"])
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
        # With sign-in on, whoever creates a project is its first admin.
        owner = None
        if request.app.state.auth is not None:
            owner = request_principal(request).user_subject
        project, created = service(request).create(
            CreateProject(
                name=body.name, display_name=body.display_name, description=body.description
            ),
            owner=owner,
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
        uow_factory: UnitOfWorkFactory = request.app.state.uow_factory
        with uow_factory() as uow:
            only = visible_project_ids(uow, request_principal(request))
        items = service(request).list(limit=limit, offset=offset, only=only)
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

    @router.put(
        "/{project}/gpu-quota",
        response_model=ProjectOut,
        responses=errors,
        summary="Set how many GPUs a project may hold (platform admins only)",
    )
    def set_gpu_quota(project: str, body: GpuQuotaIn, request: Request) -> ProjectOut:
        updated, _ = service(request).set_gpu_quota(project, body.gpus)
        return ProjectOut.from_domain(updated)

    return router


def create_app(
    uow_factory: UnitOfWorkFactory,
    clock: Clock = utc_now,
    workflow: WorkflowProvider | None = None,
    experiments: ExperimentProvider | None = None,
    serving: ServingProvider | None = None,
    metrics: MetricsProvider | None = None,
    ui: bool = True,
    telemetry: TelemetryConfig | None = None,
    auth: AuthConfig | None = None,
    platform: PlatformTelemetry | None = None,
    usage: UsageProvider | None = None,
    gateway_url: str = "",
    readiness: Callable[[], None] | None = None,
    secrets: SecretProvider | None = None,
    require_job_image_digest: bool = False,
    log_stream_key: str = "",
    log_stream_url: str = "/log-stream",
) -> FastAPI:
    """`auth=None` runs without sign-in: every caller is an anonymous platform admin. That is
    for local development, the demo and tests; production passes an `AuthConfig`."""
    app = FastAPI(
        title="ML Platform Control Plane",
        version="0.1.0",
        description="Platform API. PostgreSQL owns lifecycle state; MLflow, Argo and "
        "KServe are adapters behind it.",
        telemetry=telemetry_config(telemetry),
        dependencies=[Depends(authorize)],
    )
    app.state.uow_factory = uow_factory
    app.state.auth = auth
    add_readiness(app, readiness)
    app.state.notifications = NotificationService(uow_factory, clock)
    app.state.members = MembershipService(uow_factory, clock)
    app.state.projects = ProjectService(uow_factory, clock)
    app.state.secrets = ProjectSecretService(uow_factory, secrets, clock)
    app.state.jobs = JobService(
        uow_factory, clock, secrets, require_image_digest=require_job_image_digest
    )
    app.state.runs = RunService(uow_factory, clock)
    app.state.schedules = ScheduleService(uow_factory, clock)
    app.state.pipelines = PipelineService(uow_factory, clock)
    app.state.pipeline_runs = PipelineRunService(uow_factory, clock, experiments)
    app.state.models = ModelService(uow_factory, clock, experiments, secrets)
    app.state.evaluations = EvaluationService(uow_factory, experiments, clock)
    app.state.promotions = PromotionService(uow_factory, clock)
    app.state.deployments = DeploymentService(uow_factory, clock, experiments, serving)
    app.state.rollouts = RolloutService(uow_factory, app.state.deployments, clock)
    app.state.overview = OverviewService(uow_factory, serving, metrics, clock)
    app.state.platform = PlatformService(uow_factory, platform, clock)
    app.state.api_access = ApiAccessService(uow_factory, clock, usage)
    app.state.gateway_url = gateway_url.rstrip("/") or None
    app.state.clock = clock
    app.state.workflow = workflow
    from controlplane.api.log_stream import configure

    app.state.log_stream = configure(log_stream_key, log_stream_url)
    app.state.experiments = experiments

    @app.exception_handler(RequestValidationError)
    async def safe_validation(request: Request, exc: RequestValidationError) -> Response:
        if "/secrets" in request.url.path:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {"code": "invalid_argument", "message": "invalid secret request"}
                },
            )
        return await request_validation_exception_handler(request, exc)

    app.add_exception_handler(DomainError, handle_domain_error)
    app.add_exception_handler(DomainHttpError, handle_domain_error)
    app.include_router(_projects_router())
    app.include_router(secrets_router())
    app.include_router(jobs_router())
    app.include_router(runs_router())
    app.include_router(pipelines_router())
    app.include_router(schedules_router())
    app.include_router(pipeline_runs_router())
    app.include_router(models_router())
    app.include_router(model_versions_router())
    app.include_router(deployments_router())
    app.include_router(rollouts_router())
    app.include_router(overview_router())
    app.include_router(platform_router())
    app.include_router(api_access_router())
    app.include_router(identity_router())
    app.include_router(notifications_router())
    if auth is not None and auth.login is not None:
        app.include_router(login_router(auth))

    if ui:
        _mount_ui(app)

    @app.get("/healthz", tags=["ops"], summary="Liveness")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.add_middleware(
        AuthMiddleware, config=auth
    )  # outermost: every request is authenticated first
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
