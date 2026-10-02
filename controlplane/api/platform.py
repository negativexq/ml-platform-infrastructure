"""The platform's own health, for the Monitor page."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from controlplane.api.auth import request_principal
from controlplane.api.errors import PlatformRoute
from controlplane.application.identity import visible_project_ids
from controlplane.application.platform import Health, Inventory, PlatformHealth, PlatformService
from controlplane.application.projects import UnitOfWorkFactory
from controlplane.application.providers import PlatformSignal


class SampleOut(BaseModel):
    at: datetime
    value: float


class SeriesHealthOut(BaseModel):
    name: str = Field(description='the group (a reconciler, a system...), "" if ungrouped')
    current: float | None = Field(description="latest value, if recent; null means silent")
    status: Health
    points: list[SampleOut]


class SignalHealthOut(BaseModel):
    key: PlatformSignal
    title: str
    help: str
    unit: str = Field(description="ratio (0..1), ms, req/s or /min")
    group: str | None = Field(description="what one series stands for, if grouped")
    warn: float | None
    critical: float | None
    lower_is_worse: bool
    status: Health
    error: str | None
    series: list[SeriesHealthOut]


class InventoryOut(BaseModel):
    projects: int
    projects_not_ready: int
    runs_active: int
    runs_waiting: int = Field(description="accepted but not started")
    oldest_waiting_seconds: float | None
    runs_failed_24h: int
    deployments: int
    deployments_ready: int
    deployments_failed: int
    rollouts_active: int


class PlatformHealthOut(BaseModel):
    generated_at: datetime
    start: datetime
    end: datetime
    step_seconds: int
    available: bool = Field(description="false when no platform metrics could be read")
    error: str | None
    status: Health = Field(description="the worst status among checks that have data")
    signals: list[SignalHealthOut]
    inventory: InventoryOut

    @classmethod
    def from_view(cls, view: PlatformHealth) -> PlatformHealthOut:
        return cls(
            generated_at=view.generated_at,
            start=view.start,
            end=view.end,
            step_seconds=view.step_seconds,
            available=view.available,
            error=view.error,
            status=view.status,
            signals=[
                SignalHealthOut(
                    key=s.spec.signal,
                    title=s.spec.title,
                    help=s.spec.help,
                    unit=s.spec.unit,
                    group=s.spec.group,
                    warn=s.spec.warn,
                    critical=s.spec.critical,
                    lower_is_worse=s.spec.lower_is_worse,
                    status=s.status,
                    error=s.error,
                    series=[
                        SeriesHealthOut(
                            name=series.name,
                            current=series.current,
                            status=series.status,
                            points=[SampleOut(at=p.at, value=p.value) for p in series.points],
                        )
                        for series in s.series
                    ],
                )
                for s in view.signals
            ],
            inventory=_inventory(view.inventory),
        )


def _inventory(i: Inventory) -> InventoryOut:
    return InventoryOut(
        projects=i.projects,
        projects_not_ready=i.projects_not_ready,
        runs_active=i.runs_active,
        runs_waiting=i.runs_waiting,
        oldest_waiting_seconds=i.oldest_waiting_seconds,
        runs_failed_24h=i.runs_failed_24h,
        deployments=i.deployments,
        deployments_ready=i.deployments_ready,
        deployments_failed=i.deployments_failed,
        rollouts_active=i.rollouts_active,
    )


def platform_router() -> APIRouter:
    router = APIRouter(route_class=PlatformRoute, prefix="/platform", tags=["platform"])

    @router.get(
        "/health",
        response_model=PlatformHealthOut,
        summary="The control plane's own health: checks with thresholds and trends",
    )
    def health(
        request: Request, minutes: Annotated[int, Query(ge=5, le=1440)] = 60
    ) -> PlatformHealthOut:
        service: PlatformService = request.app.state.platform
        uow_factory: UnitOfWorkFactory = request.app.state.uow_factory
        with uow_factory() as uow:
            only = visible_project_ids(uow, request_principal(request))
        return PlatformHealthOut.from_view(service.health(minutes=minutes, only=only))

    return router
