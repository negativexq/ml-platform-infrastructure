"""Core platform entities.

Frozen dataclasses: a state change returns a new instance, so a transition can
never half-apply. Time is always passed in; the domain never reads a clock.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Self
from uuid import UUID

from controlplane.domain import states
from controlplane.domain.errors import InvalidArgument
from controlplane.domain.ids import new_id
from controlplane.domain.states import (
    DeploymentStatus,
    EvaluationStatus,
    ModelStatus,
    ProjectStatus,
    PromotionStatus,
    RunStatus,
    StepStatus,
)

# A DNS-1123 label. Project names become namespace names (`mlp-<name>`, M14),
# so the 63-character limit leaves room for the prefix.
_SLUG = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
SLUG_MIN, SLUG_MAX = 3, 40


def validate_slug(value: str, what: str = "name", min_len: int = SLUG_MIN) -> str:
    if not min_len <= len(value) <= SLUG_MAX or not _SLUG.match(value):
        raise InvalidArgument(
            f"{what} must be {min_len}-{SLUG_MAX} chars of lowercase letters, digits and "
            f"single hyphens, starting with a letter: got {value!r}"
        )
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class Project:
    id: UUID = field(default_factory=new_id)
    name: str
    display_name: str
    description: str = ""
    status: ProjectStatus = ProjectStatus.PENDING
    status_reason: str | None = None
    created_at: datetime
    updated_at: datetime

    @property
    def namespace(self) -> str:
        """Deterministic Kubernetes namespace for this project."""
        return f"mlp-{self.name}"

    @classmethod
    def create(
        cls, *, name: str, display_name: str | None, description: str, now: datetime
    ) -> Self:
        validate_slug(name)
        display = (display_name or name).strip()
        if not display or len(display) > 120:
            raise InvalidArgument("display_name must be 1-120 characters")
        if len(description) > 2000:
            raise InvalidArgument("description must be at most 2000 characters")
        return cls(
            name=name, display_name=display, description=description, created_at=now, updated_at=now
        )

    def transition_to(
        self, status: ProjectStatus, now: datetime, reason: str | None = None
    ) -> Self:
        states.PROJECT.ensure(self.status, status)
        return replace(self, status=status, status_reason=reason, updated_at=now)

    def with_reason(self, reason: str | None, now: datetime) -> Self:
        """Record why a project is stuck without changing its status."""
        return replace(self, status_reason=reason, updated_at=now)


_RESOURCE_KEYS = {"cpu", "memory", "ephemeral-storage", "nvidia.com/gpu"}
_QUANTITY = re.compile(r"^[0-9]+(\.[0-9]+)?(m|Ki|Mi|Gi|Ti|k|M|G|T)?$")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True, kw_only=True)
class JobDefinition:
    """What to run. Immutable: a different image or command is a different definition."""

    id: UUID = field(default_factory=new_id)
    project_id: UUID
    name: str
    image: str
    command: tuple[str, ...] = ()
    resources: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        project_id: UUID,
        name: str,
        image: str,
        command: tuple[str, ...],
        resources: Mapping[str, str],
        env: Mapping[str, str],
        now: datetime,
    ) -> Self:
        validate_slug(name, "job name")
        if not image.strip() or any(c.isspace() for c in image):
            raise InvalidArgument("image must be a non-empty reference without whitespace")
        for key, value in resources.items():
            if key not in _RESOURCE_KEYS:
                raise InvalidArgument(
                    f"unsupported resource {key!r}; allowed: {sorted(_RESOURCE_KEYS)}"
                )
            if not _QUANTITY.match(value):
                raise InvalidArgument(f"invalid quantity {value!r} for resource {key!r}")
        for key in env:
            if not _ENV_KEY.match(key):
                raise InvalidArgument(f"invalid environment variable name {key!r}")
        return cls(
            project_id=project_id,
            name=name,
            image=image.strip(),
            command=tuple(command),
            resources=dict(resources),
            env=dict(env),
            created_at=now,
        )


@dataclass(frozen=True, slots=True)
class StepSpec:
    name: str
    job: str  # name of a JobDefinition in the same project
    depends_on: tuple[str, ...] = ()


MAX_STEPS = 50


def validate_dag(steps: tuple[StepSpec, ...]) -> tuple[str, ...]:
    """Reject empty pipelines, duplicate names, unknown/self dependencies and
    cycles. Returns the step names in a valid execution order (declaration order
    wherever the graph allows it)."""
    if not steps:
        raise InvalidArgument("a pipeline needs at least one step")
    if len(steps) > MAX_STEPS:
        raise InvalidArgument(f"a pipeline may have at most {MAX_STEPS} steps")
    names = [step.name for step in steps]
    for name in names:
        validate_slug(name, "step name", min_len=1)
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise InvalidArgument(f"duplicate step names: {duplicates}")
    known = set(names)
    for step in steps:
        validate_slug(step.job, "job name")
        if len(set(step.depends_on)) != len(step.depends_on):
            raise InvalidArgument(f"step {step.name!r} lists a dependency twice")
        for dep in step.depends_on:
            if dep == step.name:
                raise InvalidArgument(f"step {step.name!r} depends on itself")
            if dep not in known:
                raise InvalidArgument(f"step {step.name!r} depends on unknown step {dep!r}")

    remaining = {step.name: set(step.depends_on) for step in steps}
    order: list[str] = []
    while remaining:
        ready = [name for name in names if name in remaining and not remaining[name]]
        if not ready:
            raise InvalidArgument(f"dependency cycle among steps: {sorted(remaining)}")
        for name in ready:
            order.append(name)
            del remaining[name]
        for deps in remaining.values():
            deps.difference_update(ready)
    return tuple(order)


@dataclass(frozen=True, slots=True, kw_only=True)
class PipelineDefinition:
    """One immutable version of a pipeline. Changing a pipeline creates version+1."""

    id: UUID = field(default_factory=new_id)
    project_id: UUID
    name: str
    version: int
    steps: tuple[StepSpec, ...]
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        project_id: UUID,
        name: str,
        version: int,
        steps: tuple[StepSpec, ...],
        now: datetime,
    ) -> Self:
        validate_slug(name, "pipeline name")
        validate_dag(steps)
        return cls(project_id=project_id, name=name, version=version, steps=steps, created_at=now)

    @property
    def execution_order(self) -> tuple[str, ...]:
        return validate_dag(self.steps)

    def same_content(self, other: PipelineDefinition) -> bool:
        return self.steps == other.steps


@dataclass(frozen=True, slots=True, kw_only=True)
class PipelineRun:
    id: UUID = field(default_factory=new_id)
    project_id: UUID
    pipeline_definition_id: UUID
    status: RunStatus = RunStatus.PENDING
    status_reason: str | None = None
    external_ref: str | None = None  # e.g. the Argo Workflow reference; never exposed as identity
    cancel_requested: bool = False
    commit_sha: str | None = None
    idempotency_key: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return states.PIPELINE_RUN.is_terminal(self.status)

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def transition_to(
        self,
        status: RunStatus,
        now: datetime,
        *,
        reason: str | None = None,
        external_ref: str | None = None,
    ) -> Self:
        states.PIPELINE_RUN.ensure(self.status, status)
        return replace(
            self,
            status=status,
            status_reason=reason if reason is not None else self.status_reason,
            external_ref=external_ref if external_ref is not None else self.external_ref,
            started_at=now if status is RunStatus.RUNNING else self.started_at,
            finished_at=now if states.PIPELINE_RUN.is_terminal(status) else self.finished_at,
            updated_at=now,
        )

    def with_cancel_requested(self, now: datetime) -> Self:
        return replace(self, cancel_requested=True, updated_at=now)


@dataclass(frozen=True, slots=True, kw_only=True)
class Run:
    """One execution of a JobDefinition. The platform id is the identity; the
    workflow system's reference is `external_ref` and is never shown to users."""

    id: UUID = field(default_factory=new_id)
    project_id: UUID
    job_definition_id: UUID
    status: RunStatus = RunStatus.PENDING
    status_reason: str | None = None
    exit_code: int | None = None
    external_ref: str | None = None
    cancel_requested: bool = False
    retry_of: UUID | None = None
    idempotency_key: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return states.RUN.is_terminal(self.status)

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def transition_to(
        self,
        status: RunStatus,
        now: datetime,
        *,
        reason: str | None = None,
        exit_code: int | None = None,
        external_ref: str | None = None,
    ) -> Self:
        states.RUN.ensure(self.status, status)
        return replace(
            self,
            status=status,
            status_reason=reason if reason is not None else self.status_reason,
            exit_code=exit_code if exit_code is not None else self.exit_code,
            external_ref=external_ref if external_ref is not None else self.external_ref,
            started_at=now if status is RunStatus.RUNNING else self.started_at,
            finished_at=now if states.RUN.is_terminal(status) else self.finished_at,
            updated_at=now,
        )

    def with_cancel_requested(self, now: datetime) -> Self:
        return replace(self, cancel_requested=True, updated_at=now)


@dataclass(frozen=True, slots=True, kw_only=True)
class StepRun:
    id: UUID = field(default_factory=new_id)
    pipeline_run_id: UUID
    step_name: str
    status: StepStatus = StepStatus.PENDING
    status_reason: str | None = None
    exit_code: int | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return states.STEP_RUN.is_terminal(self.status)

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

    def transition_to(
        self,
        status: StepStatus,
        now: datetime,
        *,
        reason: str | None = None,
        exit_code: int | None = None,
    ) -> Self:
        states.STEP_RUN.ensure(self.status, status)
        return replace(
            self,
            status=status,
            status_reason=reason if reason is not None else self.status_reason,
            exit_code=exit_code if exit_code is not None else self.exit_code,
            started_at=now if status is StepStatus.RUNNING else self.started_at,
            finished_at=now if states.STEP_RUN.is_terminal(status) else self.finished_at,
            updated_at=now,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Model:
    id: UUID = field(default_factory=new_id)
    project_id: UUID
    name: str
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelVersion:
    id: UUID = field(default_factory=new_id)
    model_id: UUID
    version: int
    status: ModelStatus = ModelStatus.REGISTERED
    external_ref: str | None = None  # e.g. the MLflow model version
    created_at: datetime
    updated_at: datetime

    def transition_to(self, status: ModelStatus, now: datetime) -> Self:
        states.MODEL_VERSION.ensure(self.status, status)
        return replace(self, status=status, updated_at=now)


@dataclass(frozen=True, slots=True, kw_only=True)
class Evaluation:
    id: UUID = field(default_factory=new_id)
    model_version_id: UUID
    baseline_version_id: UUID | None = None
    metrics: Mapping[str, float] = field(default_factory=dict)
    status: EvaluationStatus = EvaluationStatus.PENDING
    created_at: datetime
    updated_at: datetime

    def transition_to(self, status: EvaluationStatus, now: datetime) -> Self:
        states.EVALUATION.ensure(self.status, status)
        return replace(self, status=status, updated_at=now)


@dataclass(frozen=True, slots=True, kw_only=True)
class Promotion:
    id: UUID = field(default_factory=new_id)
    model_version_id: UUID
    previous_champion_id: UUID | None = None
    status: PromotionStatus = PromotionStatus.PENDING
    created_at: datetime
    updated_at: datetime

    def transition_to(self, status: PromotionStatus, now: datetime) -> Self:
        states.PROMOTION.ensure(self.status, status)
        return replace(self, status=status, updated_at=now)


@dataclass(frozen=True, slots=True, kw_only=True)
class Deployment:
    id: UUID = field(default_factory=new_id)
    project_id: UUID
    name: str
    status: DeploymentStatus = DeploymentStatus.PENDING
    created_at: datetime
    updated_at: datetime

    def transition_to(self, status: DeploymentStatus, now: datetime) -> Self:
        states.DEPLOYMENT.ensure(self.status, status)
        return replace(self, status=status, updated_at=now)


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentRevision:
    """Immutable: a new model version is a new revision, never an edit."""

    id: UUID = field(default_factory=new_id)
    deployment_id: UUID
    revision: int
    model_version_id: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class Endpoint:
    id: UUID = field(default_factory=new_id)
    project_id: UUID
    deployment_id: UUID
    name: str
    created_at: datetime
