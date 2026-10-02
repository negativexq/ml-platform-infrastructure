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


def validate_slug(value: str, what: str = "name") -> str:
    if not SLUG_MIN <= len(value) <= SLUG_MAX or not _SLUG.match(value):
        raise InvalidArgument(
            f"{what} must be {SLUG_MIN}-{SLUG_MAX} chars of lowercase letters, digits and "
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
    created_at: datetime
    updated_at: datetime

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

    def transition_to(self, status: ProjectStatus, now: datetime) -> Self:
        states.PROJECT.ensure(self.status, status)
        return replace(self, status=status, updated_at=now)


@dataclass(frozen=True, slots=True, kw_only=True)
class JobDefinition:
    id: UUID = field(default_factory=new_id)
    project_id: UUID
    name: str
    image: str
    command: tuple[str, ...] = ()
    resources: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StepSpec:
    name: str
    job: str  # name of a JobDefinition in the same project
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class PipelineDefinition:
    """One immutable version of a pipeline. Changing a pipeline creates version+1."""

    id: UUID = field(default_factory=new_id)
    project_id: UUID
    name: str
    version: int
    steps: tuple[StepSpec, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class PipelineRun:
    id: UUID = field(default_factory=new_id)
    project_id: UUID
    pipeline_definition_id: UUID
    status: RunStatus = RunStatus.PENDING
    external_ref: str | None = None  # e.g. the Argo Workflow uid; never exposed as identity
    created_at: datetime
    updated_at: datetime

    def transition_to(self, status: RunStatus, now: datetime) -> Self:
        states.PIPELINE_RUN.ensure(self.status, status)
        return replace(self, status=status, updated_at=now)


@dataclass(frozen=True, slots=True, kw_only=True)
class StepRun:
    id: UUID = field(default_factory=new_id)
    pipeline_run_id: UUID
    step_name: str
    status: StepStatus = StepStatus.PENDING
    created_at: datetime
    updated_at: datetime

    def transition_to(self, status: StepStatus, now: datetime) -> Self:
        states.STEP_RUN.ensure(self.status, status)
        return replace(self, status=status, updated_at=now)


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
