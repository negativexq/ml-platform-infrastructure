from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response, status

from controlplane.api.errors import PlatformRoute
from controlplane.api.schemas import ErrorOut
from controlplane.api.schemas_models import (
    DiscoveryOut,
    ModelCreate,
    ModelList,
    ModelOut,
    ModelVersionOut,
    ModelVersionSummary,
    ThresholdsUpdate,
    VersionList,
)
from controlplane.application.models import EvaluationService, ModelService, PromotionService

_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorOut},
    409: {"model": ErrorOut},
    422: {"model": ErrorOut},
}


def models_router() -> APIRouter:
    router = APIRouter(
        route_class=PlatformRoute, prefix="/projects/{project}/models", tags=["models"]
    )

    def svc(request: Request) -> ModelService:
        service: ModelService = request.app.state.models
        return service

    @router.post(
        "",
        response_model=ModelOut,
        status_code=status.HTTP_201_CREATED,
        responses={
            200: {"model": ModelOut, "description": "Identical model already exists"},
            **_ERRORS,
        },
        summary="Create a model with its acceptance thresholds (idempotent)",
    )
    def create_model(
        project: str, body: ModelCreate, request: Request, response: Response
    ) -> ModelOut:
        view, created = svc(request).create(
            project, body.name, {k: t.to_domain() for k, t in body.thresholds.items()}
        )
        if not created:
            response.status_code = status.HTTP_200_OK
        return ModelOut.from_view(view)

    @router.get("", response_model=ModelList, responses=_ERRORS, summary="List models")
    def list_models(project: str, request: Request) -> ModelList:
        return ModelList(items=[ModelOut.from_view(v) for v in svc(request).list(project)])

    @router.get("/{name}", response_model=ModelOut, responses=_ERRORS, summary="Get a model")
    def get_model(project: str, name: str, request: Request) -> ModelOut:
        return ModelOut.from_view(svc(request).get(project, name))

    @router.put(
        "/{name}/thresholds",
        response_model=ModelOut,
        responses=_ERRORS,
        summary="Replace the thresholds used by future evaluations",
    )
    def set_thresholds(
        project: str, name: str, body: ThresholdsUpdate, request: Request
    ) -> ModelOut:
        view = svc(request).set_thresholds(
            project, name, {k: t.to_domain() for k, t in body.thresholds.items()}
        )
        return ModelOut.from_view(view)

    @router.get(
        "/{name}/versions", response_model=VersionList, responses=_ERRORS, summary="List versions"
    )
    def list_versions(project: str, name: str, request: Request) -> VersionList:
        view = svc(request).get(project, name)
        return VersionList(items=[ModelVersionSummary.from_domain(v) for v in view.versions])

    @router.post(
        "/{name}/discover",
        response_model=DiscoveryOut,
        responses=_ERRORS,
        summary="Register versions found in the model registry (idempotent)",
    )
    def discover(project: str, name: str, request: Request) -> DiscoveryOut:
        return DiscoveryOut.from_domain(svc(request).discover(project, name))

    return router


def model_versions_router() -> APIRouter:
    router = APIRouter(route_class=PlatformRoute, prefix="/model-versions", tags=["models"])

    @router.get(
        "/{version_id}",
        response_model=ModelVersionOut,
        responses=_ERRORS,
        summary="Version with its evaluations and promotions",
    )
    def get_version(version_id: UUID, request: Request) -> ModelVersionOut:
        models: ModelService = request.app.state.models
        return ModelVersionOut.from_view(models.get_version(version_id))

    @router.post(
        "/{version_id}/evaluate",
        response_model=ModelVersionOut,
        responses={503: {"model": ErrorOut}, **_ERRORS},
        summary="Evaluate against the model's thresholds: CANDIDATE or REJECTED",
    )
    def evaluate(version_id: UUID, request: Request) -> ModelVersionOut:
        service: EvaluationService | None = request.app.state.evaluations
        if service is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "no model registry configured")
        return ModelVersionOut.from_view(service.evaluate(version_id))

    @router.post(
        "/{version_id}/promote",
        response_model=ModelVersionOut,
        responses=_ERRORS,
        summary="Promote a CANDIDATE to CHAMPION (rejected versions cannot be promoted)",
    )
    def promote(version_id: UUID, request: Request) -> ModelVersionOut:
        promotions: PromotionService = request.app.state.promotions
        return ModelVersionOut.from_view(promotions.promote(version_id))

    return router
