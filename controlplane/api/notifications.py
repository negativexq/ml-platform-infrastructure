"""Personal lifecycle notifications; all permissions come from the signed-in caller."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from controlplane.api.auth import request_principal
from controlplane.api.errors import PlatformRoute
from controlplane.application.notifications import NotificationService


class NotificationOut(BaseModel):
    id: str
    kind: str
    title: str
    project: str
    project_label: str
    resource_type: str
    resource_id: str
    resource_name: str
    status: str
    reason: str | None
    occurred_at: datetime
    needs_attention: bool
    endpoint_name: str | None
    read: bool


class NotificationList(BaseModel):
    items: list[NotificationOut]
    attention_count: int
    unread_count: int
    truncated: bool


class NotificationReadIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]] = Field(
        default_factory=list, max_length=500
    )
    all: bool = False


class NotificationReadOut(BaseModel):
    marked: int


def notifications_router() -> APIRouter:
    router = APIRouter(
        route_class=PlatformRoute, prefix="/me/notifications", tags=["notifications"]
    )

    @router.get(
        "", response_model=NotificationList, summary="Current issues and recent rollout outcomes"
    )
    def list_notifications(request: Request) -> NotificationList:
        service: NotificationService = request.app.state.notifications
        items = service.list(request_principal(request))
        return NotificationList(
            items=[NotificationOut(**vars(n)) for n in items[:500]],
            attention_count=sum(n.needs_attention for n in items),
            unread_count=sum(not n.read for n in items),
            truncated=len(items) > 500,
        )

    @router.post(
        "/read", response_model=NotificationReadOut, summary="Mark your notifications read"
    )
    def mark_read(body: NotificationReadIn, request: Request) -> NotificationReadOut:
        service: NotificationService = request.app.state.notifications
        return NotificationReadOut(
            marked=service.mark_read(
                request_principal(request), body.ids, all_notifications=body.all
            )
        )

    return router
