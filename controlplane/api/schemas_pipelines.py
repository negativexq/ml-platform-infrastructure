from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from controlplane.application.pipeline_runs import PipelineRunView, TrackedRun
from controlplane.domain.entities import PipelineDefinition, PipelineRun, StepRun
from controlplane.domain.states import RunStatus, StepStatus


class StepIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    job: str = Field(description="Name of a job definition in the same project")
    depends_on: list[str] = Field(default_factory=list)


class PipelineCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    steps: list[StepIn]


class StepDefOut(BaseModel):
    name: str
    job: str
    depends_on: list[str]


class PipelineOut(BaseModel):
    id: UUID
    project_id: UUID
    name: str
    version: int
    steps: list[StepDefOut]
    created_at: datetime

    @classmethod
    def from_domain(cls, d: PipelineDefinition) -> PipelineOut:
        return cls(
            id=d.id,
            project_id=d.project_id,
            name=d.name,
            version=d.version,
            steps=[
                StepDefOut(name=s.name, job=s.job, depends_on=list(s.depends_on)) for s in d.steps
            ],
            created_at=d.created_at,
        )


class PipelineList(BaseModel):
    items: list[PipelineOut]


class PipelineRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commit_sha: str | None = Field(default=None, max_length=64)
    timeout_seconds: int = Field(3600, ge=1, le=604800)


class StepRunOut(BaseModel):
    step: str
    status: StepStatus
    status_reason: str | None
    exit_code: int | None
    depends_on: list[str]
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None

    @classmethod
    def from_domain(cls, step: StepRun, depends_on: list[str]) -> StepRunOut:
        return cls(
            step=step.step_name,
            status=step.status,
            status_reason=step.status_reason,
            exit_code=step.exit_code,
            depends_on=depends_on,
            started_at=step.started_at,
            finished_at=step.finished_at,
            duration_seconds=step.duration_seconds,
        )


class PipelineRunSummary(BaseModel):
    """Omits the workflow system's references: the platform id is the only identity."""

    id: UUID
    project_id: UUID
    pipeline_definition_id: UUID
    pipeline: str | None = None
    pipeline_version: int | None = None
    status: RunStatus
    status_reason: str | None
    cancel_requested: bool
    timeout_seconds: int
    workflow_cleaned_at: datetime | None
    commit_sha: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None

    @classmethod
    def from_domain(
        cls, run: PipelineRun, label: tuple[str, int] | None = None
    ) -> PipelineRunSummary:
        return cls(
            id=run.id,
            project_id=run.project_id,
            pipeline_definition_id=run.pipeline_definition_id,
            pipeline=label[0] if label else None,
            pipeline_version=label[1] if label else None,
            status=run.status,
            status_reason=run.status_reason,
            cancel_requested=run.cancel_requested,
            timeout_seconds=run.timeout_seconds,
            workflow_cleaned_at=run.workflow_cleaned_at,
            commit_sha=run.commit_sha,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            duration_seconds=run.duration_seconds,
        )


class PipelineRunOut(PipelineRunSummary):
    pipeline: str  # always known on a single run
    pipeline_version: int
    steps: list[StepRunOut]

    @classmethod
    def from_view(cls, view: PipelineRunView) -> PipelineRunOut:
        deps = {s.name: list(s.depends_on) for s in view.definition.steps}
        return cls(
            **PipelineRunSummary.from_domain(
                view.run, (view.definition.name, view.definition.version)
            ).model_dump(),
            steps=[StepRunOut.from_domain(s, deps[s.step_name]) for s in view.steps],
        )


class PipelineRunList(BaseModel):
    items: list[PipelineRunSummary]
    limit: int
    offset: int


class TrackedRunOut(BaseModel):
    step: str | None
    params: dict[str, str]
    metrics: dict[str, float]
    tags: dict[str, str]
    artifact_uri: str | None

    @classmethod
    def from_domain(cls, tracked: TrackedRun) -> TrackedRunOut:
        return cls(
            step=tracked.step,
            params=dict(tracked.run.params),
            metrics=dict(tracked.run.metrics),
            tags=dict(tracked.run.tags),
            artifact_uri=tracked.run.artifact_uri,
        )


class TrackingOut(BaseModel):
    pipeline_run_id: UUID
    runs: list[TrackedRunOut]
