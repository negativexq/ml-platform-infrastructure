from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from controlplane.domain.entities import JobDefinition, Run
from controlplane.domain.states import RunStatus


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    image: str
    command: list[str] = Field(default_factory=list)
    resources: dict[str, str] = Field(
        default_factory=dict, examples=[{"cpu": "2", "memory": "4Gi"}]
    )
    env: dict[str, str] = Field(default_factory=dict)


class JobOut(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    image: str
    command: list[str]
    resources: dict[str, str]
    env: dict[str, str]
    created_at: datetime

    @classmethod
    def from_domain(cls, job: JobDefinition) -> JobOut:
        return cls(
            id=job.id,
            project_id=job.project_id,
            name=job.name,
            image=job.image,
            command=list(job.command),
            resources=dict(job.resources),
            env=dict(job.env),
            created_at=job.created_at,
        )


class JobList(BaseModel):
    items: list[JobOut]


class RunOut(BaseModel):
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

    @classmethod
    def from_domain(cls, run: Run, job: str | None = None) -> RunOut:
        return cls(
            id=run.id,
            project_id=run.project_id,
            job_id=run.job_definition_id,
            job=job,
            status=run.status,
            status_reason=run.status_reason,
            exit_code=run.exit_code,
            cancel_requested=run.cancel_requested,
            retry_of=run.retry_of,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            duration_seconds=run.duration_seconds,
        )


class RunList(BaseModel):
    items: list[RunOut]
    limit: int
    offset: int
