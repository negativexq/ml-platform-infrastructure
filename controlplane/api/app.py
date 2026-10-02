from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, FastAPI, Query, Request, Response, status
from fastapi.responses import JSONResponse

from controlplane.api.schemas import ErrorOut, ProjectCreate, ProjectList, ProjectOut
from controlplane.application.projects import (
    Clock,
    CreateProject,
    ProjectService,
    UnitOfWorkFactory,
    utc_now,
)
from controlplane.domain.errors import (
    AlreadyExists,
    Conflict,
    DomainError,
    IllegalTransition,
    InvalidArgument,
    NotFound,
)

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

    return router


def create_app(uow_factory: UnitOfWorkFactory, clock: Clock = utc_now) -> FastAPI:
    app = FastAPI(
        title="ML Platform Control Plane",
        version="0.1.0",
        description="Platform API. PostgreSQL owns lifecycle state; MLflow, Argo and "
        "KServe are adapters behind it.",
    )
    app.state.projects = ProjectService(uow_factory, clock)
    app.add_exception_handler(DomainError, _domain_error_handler)
    app.include_router(_projects_router())

    @app.get("/healthz", tags=["ops"], summary="Liveness")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
