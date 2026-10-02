from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, status

from controlplane.api.errors import PlatformRoute
from controlplane.api.schemas import ErrorOut
from controlplane.api.schemas_deployments import DeploymentOut
from controlplane.api.schemas_rollouts import (
    RollbackRequest,
    RolloutCreate,
    RolloutList,
    RolloutOut,
)
from controlplane.application.deployments import DeploymentService
from controlplane.application.rollouts import RolloutService

_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorOut},
    409: {"model": ErrorOut},
    422: {"model": ErrorOut},
}


def rollouts_router() -> APIRouter:
    router = APIRouter(route_class=PlatformRoute, tags=["rollouts"])

    def svc(request: Request) -> RolloutService:
        service: RolloutService = request.app.state.rollouts
        return service

    @router.post(
        "/projects/{project}/deployments/{name}/rollouts",
        response_model=RolloutOut,
        status_code=status.HTTP_202_ACCEPTED,
        responses=_ERRORS,
        summary="Canary a model version onto a READY deployment, gated step by step",
    )
    def start_rollout(project: str, name: str, body: RolloutCreate, request: Request) -> RolloutOut:
        view = svc(request).start(
            project,
            name,
            body.model,
            body.version,
            steps=body.steps,
            gate=body.gate.to_domain() if body.gate else None,
        )
        return RolloutOut.from_view(view)

    @router.get(
        "/projects/{project}/deployments/{name}/rollouts",
        response_model=RolloutList,
        responses=_ERRORS,
        summary="Rollouts of a deployment, newest first",
    )
    def list_rollouts(project: str, name: str, request: Request) -> RolloutList:
        return RolloutList(
            items=[RolloutOut.from_view(v) for v in svc(request).list(project, name)]
        )

    @router.get(
        "/rollouts/{rollout_id}",
        response_model=RolloutOut,
        responses=_ERRORS,
        summary="Get a rollout",
    )
    def get_rollout(rollout_id: UUID, request: Request) -> RolloutOut:
        return RolloutOut.from_view(svc(request).get(rollout_id))

    @router.post(
        "/rollouts/{rollout_id}/abort",
        response_model=RolloutOut,
        status_code=status.HTTP_202_ACCEPTED,
        responses=_ERRORS,
        summary="Abort a rollout; all traffic returns to the stable revision (idempotent)",
    )
    def abort_rollout(rollout_id: UUID, request: Request) -> RolloutOut:
        return RolloutOut.from_view(svc(request).abort(rollout_id))

    @router.post(
        "/projects/{project}/deployments/{name}/rollback",
        response_model=DeploymentOut,
        status_code=status.HTTP_202_ACCEPTED,
        responses=_ERRORS,
        summary="Serve an earlier revision again (default: the previous one)",
    )
    def rollback(
        project: str, name: str, request: Request, body: RollbackRequest | None = None
    ) -> DeploymentOut:
        deployments: DeploymentService = request.app.state.deployments
        view = deployments.rollback(project, name, body.to_revision if body else None)
        return DeploymentOut.from_view(view)

    return router
