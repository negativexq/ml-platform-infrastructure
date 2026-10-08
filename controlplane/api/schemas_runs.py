from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from controlplane.api.secrets import SecretRefsIn
from controlplane.domain.entities import JobDefinition, Run
from controlplane.domain.states import RunStatus


class JobCreate(BaseModel):
    parameter_schema: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")

    secret_refs: SecretRefsIn = Field(default_factory=SecretRefsIn)
    name: str
    image: str
    command: list[str] = Field(default_factory=list)
    resources: dict[str, str] = Field(
        default_factory=dict, examples=[{"cpu": "2", "memory": "4Gi"}]
    )
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(3600, ge=1, le=604800)


class JobOut(BaseModel):
    batch_spec: dict[str, Any] = Field(default_factory=dict)
    monitoring_spec: dict[str, Any] = Field(default_factory=dict)
    parameter_schema: dict[str, Any] = Field(default_factory=dict)

    id: UUID
    project_id: UUID
    secret_refs: SecretRefsIn = Field(default_factory=SecretRefsIn)
    name: str
    image: str
    command: list[str]
    resources: dict[str, str]
    env: dict[str, str]
    created_at: datetime
    timeout_seconds: int

    @classmethod
    def from_domain(cls, job: JobDefinition) -> JobOut:
        return cls(
            id=job.id,
            project_id=job.project_id,
            name=job.name,
            secret_refs=SecretRefsIn.from_domain(job.secret_refs),
            image=job.image,
            command=list(job.command),
            resources=dict(job.resources),
            parameter_schema=dict(job.parameter_schema),
            batch_spec=dict(job.batch_spec),
            monitoring_spec=dict(job.monitoring_spec),
            env=dict(job.env),
            created_at=job.created_at,
            timeout_seconds=job.timeout_seconds,
        )


class JobList(BaseModel):
    items: list[JobOut]


class RunOut(BaseModel):
    parameters: dict[str, Any] = Field(default_factory=dict)

    """Deliberately omits the workflow system's references (workflow uid, pod
    name, namespace): the platform id is the only identity users deal with."""

    id: UUID
    project_id: UUID
    job_id: UUID
    job: str | None = None
    status: RunStatus
    status_reason: str | None
    exit_code: int | None
    cancel_requested: bool
    retry_of: UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None
    timeout_seconds: int
    workflow_cleaned_at: datetime | None

    @classmethod
    def from_domain(cls, run: Run, job: str | None = None) -> RunOut:
        return cls(
            id=run.id,
            project_id=run.project_id,
            job_id=run.job_definition_id,
            job=job,
            parameters=dict(run.parameters),
            status=run.status,
            status_reason=run.status_reason,
            exit_code=run.exit_code,
            cancel_requested=run.cancel_requested,
            retry_of=run.retry_of,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            duration_seconds=run.duration_seconds,
            timeout_seconds=run.timeout_seconds,
            workflow_cleaned_at=run.workflow_cleaned_at,
        )


class RunList(BaseModel):
    items: list[RunOut]
    limit: int
    offset: int


class RunCreate(BaseModel):
    parameters: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")
    timeout_seconds: int | None = Field(None, ge=1, le=604800)
