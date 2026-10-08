from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from controlplane.api.errors import PlatformRoute
from controlplane.api.model_monitoring import MonitoringCreate


class RuleCreate(MonitoringCreate):
    # Catalog selection replaces the manual observed/feedback UUIDs.
    observed_dataset_id: None = Field(default=None, exclude=True)
    feedback_dataset_id: None = Field(default=None, exclude=True)
    observed_dataset_name: str = Field(max_length=40)
    feedback_dataset_name: str | None = Field(default=None, max_length=40)
    feedback_deadline_seconds: int = Field(default=86400, ge=60, le=604800)
    enabled: bool = True


class RuleOut(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    model_version_id: UUID
    reference_dataset_id: UUID
    observed_dataset_name: str
    feedback_dataset_name: str | None
    options: dict[str, Any]
    image: str
    feedback_deadline_seconds: int
    enabled: bool
    revision: int
    created_at: datetime
    updated_at: datetime


class RuleToggle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    revision: int = Field(ge=1)


class MonitoringExecutionOut(BaseModel):
    id: UUID
    rule_id: UUID
    observed_dataset_id: UUID
    status: Literal["WAITING_FEEDBACK", "DISPATCHED", "FAILED", "SKIPPED"]
    reason: str | None
    job_definition_id: UUID | None
    job_run_id: UUID | None
    snapshot: dict[str, Any]
    deadline_at: datetime
    created_at: datetime
    updated_at: datetime


def public_rule(rule):
    return {**asdict(rule), "options": rule.options["parameters"]}


def automation_router():
    router = APIRouter(
        prefix="/projects/{project}/model-monitoring/rules",
        route_class=PlatformRoute,
        tags=["model monitoring"],
    )

    @router.post("", response_model=RuleOut, status_code=201)
    def create(project: str, body: RuleCreate, request: Request):
        return public_rule(
            request.app.state.monitoring_automation.create(project, **body.model_dump())
        )

    @router.get("", response_model=list[RuleOut])
    def list_rules(project: str, request: Request):
        return [public_rule(rule) for rule in request.app.state.monitoring_automation.list(project)]

    @router.patch("/{id}", response_model=RuleOut)
    def toggle(project: str, id: UUID, body: RuleToggle, request: Request):
        return public_rule(
            request.app.state.monitoring_automation.set_enabled(project, id, **body.model_dump())
        )

    @router.get("/{id}/executions", response_model=list[MonitoringExecutionOut])
    def executions(
        project: str,
        id: UUID,
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        # Expose the immutable workload specification, never internal secret metadata.
        return [
            {**asdict(item), "snapshot": item.snapshot.get("monitoring_spec", {})}
            for item in request.app.state.monitoring_automation.executions(
                project, id, limit, offset
            )
        ]

    return router
