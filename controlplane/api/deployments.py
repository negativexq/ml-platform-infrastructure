from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from controlplane.api.errors import PlatformRoute
from controlplane.api.schemas import ErrorOut
from controlplane.api.schemas_deployments import (
    ChatRequest,
    DeploymentCreate,
    DeploymentList,
    DeploymentOut,
    EndpointOut,
    PredictRequest,
    PredictResponse,
    RevisionCreate,
)
from controlplane.application.deployments import DeploymentService

_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorOut},
    409: {"model": ErrorOut},
    422: {"model": ErrorOut},
}


def deployments_router() -> APIRouter:
    router = APIRouter(
        route_class=PlatformRoute, prefix="/projects/{project}", tags=["deployments"]
    )

    def svc(request: Request) -> DeploymentService:
        service: DeploymentService = request.app.state.deployments
        return service

    @router.post(
        "/deployments",
        response_model=DeploymentOut,
        status_code=status.HTTP_201_CREATED,
        responses={200: {"model": DeploymentOut, "description": "Already exists"}, **_ERRORS},
        summary="Create a deployment and its endpoint (idempotent)",
    )
    def create_deployment(
        project: str, body: DeploymentCreate, request: Request, response: Response
    ) -> DeploymentOut:
        view, created = svc(request).create(project, body.name)
        if not created:
            response.status_code = status.HTTP_200_OK
        return DeploymentOut.from_view(view)

    @router.get(
        "/deployments", response_model=DeploymentList, responses=_ERRORS, summary="List deployments"
    )
    def list_deployments(project: str, request: Request) -> DeploymentList:
        return DeploymentList(
            items=[DeploymentOut.from_view(v) for v in svc(request).list(project)]
        )

    @router.get(
        "/deployments/{name}",
        response_model=DeploymentOut,
        responses=_ERRORS,
        summary="Deployment with its revisions and endpoint",
    )
    def get_deployment(project: str, name: str, request: Request) -> DeploymentOut:
        return DeploymentOut.from_view(svc(request).get(project, name))

    @router.post(
        "/deployments/{name}/revisions",
        response_model=DeploymentOut,
        status_code=status.HTTP_202_ACCEPTED,
        responses={
            200: {"model": DeploymentOut, "description": "Already the latest revision"},
            **_ERRORS,
        },
        summary="Serve a model version as a new immutable revision (CANDIDATE or CHAMPION only)",
    )
    def create_revision(
        project: str, name: str, body: RevisionCreate, request: Request, response: Response
    ) -> DeploymentOut:
        view, created = svc(request).deploy(project, name, body.model, body.version)
        if not created:
            response.status_code = status.HTTP_200_OK
        return DeploymentOut.from_view(view)

    @router.get(
        "/endpoints/{name}",
        response_model=EndpointOut,
        responses=_ERRORS,
        summary="Get an endpoint",
    )
    def get_endpoint(project: str, name: str, request: Request) -> EndpointOut:
        return EndpointOut.from_domain(svc(request).get_endpoint(project, name))

    @router.post(
        "/endpoints/{name}/predict",
        response_model=PredictResponse,
        responses={502: {"model": ErrorOut}, **_ERRORS},
        summary="Inference through the platform (409 unless the endpoint is READY)",
    )
    def predict(project: str, name: str, body: PredictRequest, request: Request) -> PredictResponse:
        try:
            return dict(svc(request).predict(project, name, body.model_dump()))
        except ConnectionError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    @router.post(
        "/endpoints/{name}/chat",
        responses={502: {"model": ErrorOut}, **_ERRORS},
        summary="One chat completion from an LLM endpoint through the platform (the playground)",
    )
    def chat(project: str, name: str, body: ChatRequest, request: Request) -> dict[str, Any]:
        try:
            answer = svc(request).chat(project, name, body.model_dump(exclude_none=True))
        except ConnectionError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        return dict(answer)

    return router
