"""Core platform entities.

Frozen dataclasses: a state change returns a new instance, so a transition can
never half-apply. Time is always passed in; the domain never reads a clock.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from controlplane.domain import states
from controlplane.domain.errors import InvalidArgument
from controlplane.domain.ids import new_id
from controlplane.domain.states import (
    DeploymentStatus,
    EndpointStatus,
    EvaluationStatus,
    ModelStatus,
    ProjectStatus,
    PromotionStatus,
    RolloutStatus,
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
    traceparent: str | None = (
        None  # the request that created it; lets reconcilers continue its trace
    )
    created_at: datetime
    updated_at: datetime

    @property
    def namespace(self) -> str:
        """Deterministic Kubernetes namespace for this project."""
        return f"mlp-{self.name}"

    @classmethod
    def create(
        cls,
        *,
        name: str,
        display_name: str | None,
        description: str,
        now: datetime,
        traceparent: str | None = None,
    ) -> Self:
        validate_slug(name)
        display = (display_name or name).strip()
        if not display or len(display) > 120:
            raise InvalidArgument("display_name must be 1-120 characters")
        if len(description) > 2000:
            raise InvalidArgument("description must be at most 2000 characters")
        return cls(
            name=name,
            display_name=display,
            description=description,
            traceparent=traceparent,
            created_at=now,
            updated_at=now,
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
    traceparent: str | None = None
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
    traceparent: str | None = None
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


@dataclass(frozen=True, slots=True)
class Threshold:
    """Acceptance bounds for one metric. At least one bound is required."""

    min: float | None = None
    max: float | None = None

    def __post_init__(self) -> None:
        if self.min is None and self.max is None:
            raise InvalidArgument("a threshold needs a min, a max, or both")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise InvalidArgument(f"threshold min {self.min} is above max {self.max}")

    def check(self, value: float | None) -> bool:
        if value is None:
            return False  # a metric that was never reported cannot pass
        return (self.min is None or value >= self.min) and (self.max is None or value <= self.max)


@dataclass(frozen=True, slots=True)
class Check:
    """The outcome of one threshold against one metric."""

    metric: str
    value: float | None
    threshold: Threshold
    passed: bool


def run_checks(
    metrics: Mapping[str, float], thresholds: Mapping[str, Threshold]
) -> tuple[Check, ...]:
    return tuple(
        Check(name, metrics.get(name), thresholds[name], thresholds[name].check(metrics.get(name)))
        for name in sorted(thresholds)
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Model:
    """A logical model of a project. Its versions live in the platform; the
    registry (MLflow) is only where the artifacts are kept."""

    id: UUID = field(default_factory=new_id)
    project_id: UUID
    name: str
    thresholds: Mapping[str, Threshold] = field(default_factory=dict)
    # Set while the registry's aliases disagree with platform state; None when in sync.
    alias_drift: str | None = None
    created_at: datetime

    @classmethod
    def create(
        cls,
        *,
        project_id: UUID,
        name: str,
        thresholds: Mapping[str, Threshold],
        now: datetime,
    ) -> Self:
        validate_slug(name, "model name")
        for metric in thresholds:
            if not metric.strip():
                raise InvalidArgument("metric names must not be empty")
        return cls(project_id=project_id, name=name, thresholds=dict(thresholds), created_at=now)

    def with_thresholds(self, thresholds: Mapping[str, Threshold]) -> Self:
        for metric in thresholds:
            if not metric.strip():
                raise InvalidArgument("metric names must not be empty")
        return replace(self, thresholds=dict(thresholds))

    def with_alias_drift(self, drift: str | None) -> Self:
        return replace(self, alias_drift=drift)

    def registry_name(self, project_name: str) -> str:
        """Deterministic name in the model registry (names there are global)."""
        return f"{project_name}-{self.name}"


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelVersion:
    id: UUID = field(default_factory=new_id)
    model_id: UUID
    version: int
    status: ModelStatus = ModelStatus.REGISTERED
    external_ref: str | None = None  # the registry's version number; unique per model
    source_pipeline_run_id: UUID | None = None  # lineage: which pipeline run produced it
    created_at: datetime
    updated_at: datetime

    def transition_to(self, status: ModelStatus, now: datetime) -> Self:
        states.MODEL_VERSION.ensure(self.status, status)
        return replace(self, status=status, updated_at=now)


@dataclass(frozen=True, slots=True, kw_only=True)
class Evaluation:
    id: UUID = field(default_factory=new_id)
    model_version_id: UUID
    baseline_version_id: UUID | None = None  # the champion at evaluation time
    metrics: Mapping[str, float] = field(default_factory=dict)
    checks: tuple[Check, ...] = ()
    status: EvaluationStatus = EvaluationStatus.PENDING
    created_at: datetime
    updated_at: datetime

    def transition_to(self, status: EvaluationStatus, now: datetime) -> Self:
        states.EVALUATION.ensure(self.status, status)
        return replace(self, status=status, updated_at=now)

    def with_result(
        self, metrics: Mapping[str, float], checks: tuple[Check, ...], now: datetime
    ) -> Self:
        return replace(self, metrics=dict(metrics), checks=checks, updated_at=now)


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


#: Model version states a deployment may serve: evaluation has passed.
DEPLOYABLE = frozenset({ModelStatus.CANDIDATE, ModelStatus.CHAMPION})


@dataclass(frozen=True, slots=True, kw_only=True)
class Deployment:
    """What should be serving. `desired_revision` is the platform's intent; the
    reconciler makes it real and records the revision it observed as live."""

    id: UUID = field(default_factory=new_id)
    project_id: UUID
    name: str
    status: DeploymentStatus = DeploymentStatus.PENDING
    status_reason: str | None = None
    desired_revision: int | None = None
    active_revision: int | None = None
    # The request that last changed what should be serving (create / new revision / rollback).
    traceparent: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(
        cls, *, project_id: UUID, name: str, now: datetime, traceparent: str | None = None
    ) -> Self:
        validate_slug(name, "deployment name")
        return cls(
            project_id=project_id,
            name=name,
            traceparent=traceparent,
            created_at=now,
            updated_at=now,
        )

    def transition_to(
        self, status: DeploymentStatus, now: datetime, reason: str | None = None
    ) -> Self:
        states.DEPLOYMENT.ensure(self.status, status)
        return replace(self, status=status, status_reason=reason, updated_at=now)

    def with_desired(self, revision: int, now: datetime, traceparent: str | None = None) -> Self:
        return replace(
            self,
            desired_revision=revision,
            traceparent=traceparent if traceparent is not None else self.traceparent,
            updated_at=now,
        )

    def with_active(self, revision: int | None, now: datetime) -> Self:
        return replace(self, active_revision=revision, updated_at=now)


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentRevision:
    """Immutable: a new model version is a new revision, never an edit. The model
    artifact location is resolved once, at creation, so the revision keeps meaning
    the same thing even if the registry changes later."""

    id: UUID = field(default_factory=new_id)
    deployment_id: UUID
    revision: int
    model_version_id: UUID
    model_uri: str
    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class Endpoint:
    id: UUID = field(default_factory=new_id)
    project_id: UUID
    deployment_id: UUID
    name: str
    status: EndpointStatus = EndpointStatus.PENDING
    url: str | None = None
    created_at: datetime
    updated_at: datetime

    def transition_to(self, status: EndpointStatus, now: datetime, url: str | None = None) -> Self:
        states.ENDPOINT.ensure(self.status, status)
        return replace(
            self, status=status, url=url if url is not None else self.url, updated_at=now
        )


@dataclass(frozen=True, slots=True)
class RolloutGate:
    """What a canary must satisfy at every step before it gets more traffic."""

    max_error_rate: float = 0.01  # 0..1
    max_p95_latency_ms: float = 500.0
    min_requests: int = 20  # below this there is too little evidence to judge
    step_seconds: int = 60  # how long each step observes before it may advance
    ready_timeout_seconds: int = 600  # how long the canary may take to load

    def __post_init__(self) -> None:
        if not 0.0 <= self.max_error_rate <= 1.0:
            raise InvalidArgument("max_error_rate must be between 0 and 1")
        if self.max_p95_latency_ms <= 0:
            raise InvalidArgument("max_p95_latency_ms must be positive")
        if self.min_requests < 1 or self.step_seconds < 0 or self.ready_timeout_seconds < 1:
            raise InvalidArgument(
                "min_requests >= 1, step_seconds >= 0, ready_timeout_seconds >= 1"
            )


class Verdict(StrEnum):
    PASS = "PASS"
    WAIT = "WAIT"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class GateResult:
    verdict: Verdict
    reason: str


def evaluate_gate(
    gate: RolloutGate,
    *,
    requests: float | None,
    error_rate: float | None,
    p95_latency_ms: float | None,
    elapsed_seconds: float,
) -> GateResult:
    """Pure decision for one canary step.

    A *breach* fails immediately once there is enough traffic to be sure. A *clean*
    step passes only after it has observed for `step_seconds`. If that time passes
    without enough traffic to judge, the gate fails closed: a canary nobody exercised
    has proven nothing.
    """
    enough = requests is not None and requests >= gate.min_requests
    if enough and error_rate is not None and error_rate > gate.max_error_rate:
        return GateResult(
            Verdict.FAIL, f"error rate {error_rate:.2%} exceeds {gate.max_error_rate:.2%}"
        )
    if enough and p95_latency_ms is not None and p95_latency_ms > gate.max_p95_latency_ms:
        return GateResult(
            Verdict.FAIL,
            f"p95 latency {p95_latency_ms:.0f} ms exceeds {gate.max_p95_latency_ms:.0f} ms",
        )
    if elapsed_seconds < gate.step_seconds:
        return GateResult(Verdict.WAIT, f"observing ({elapsed_seconds:.0f}/{gate.step_seconds}s)")
    if not enough:
        seen = 0 if requests is None else int(requests)
        return GateResult(
            Verdict.FAIL,
            f"only {seen} requests after {gate.step_seconds}s; need {gate.min_requests} to judge",
        )
    if error_rate is None or p95_latency_ms is None:
        return GateResult(Verdict.FAIL, "traffic was seen but error rate / latency are unavailable")
    return GateResult(Verdict.PASS, "within error-rate and latency limits")


DEFAULT_STEPS = (10, 25, 50, 100)


def validate_steps(steps: tuple[int, ...]) -> tuple[int, ...]:
    if not steps:
        raise InvalidArgument("a rollout needs at least one step")
    if steps[-1] != 100:
        raise InvalidArgument("the last rollout step must be 100")
    if any(not 1 <= p <= 100 for p in steps) or any(
        a >= b for a, b in zip(steps, steps[1:], strict=False)
    ):
        raise InvalidArgument("steps must be strictly increasing percentages between 1 and 100")
    return steps


@dataclass(frozen=True, slots=True, kw_only=True)
class Rollout:
    """Shifting a deployment from its stable revision to a canary revision, step by
    step. The stable revision keeps serving the rest of the traffic throughout."""

    id: UUID = field(default_factory=new_id)
    deployment_id: UUID
    from_revision: int  # stable
    to_revision: int  # canary
    model_version_id: UUID
    status: RolloutStatus = RolloutStatus.PENDING
    status_reason: str | None = None
    steps: tuple[int, ...] = DEFAULT_STEPS
    current_step: int = -1  # index into `steps`; -1 until the first step is applied
    gate: RolloutGate = field(default_factory=RolloutGate)
    step_started_at: datetime | None = None  # when the canary became ready at this step
    abort_requested: bool = False
    traceparent: str | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None

    @property
    def percent(self) -> int:
        """Traffic share of the canary right now."""
        return 0 if self.current_step < 0 else self.steps[self.current_step]

    @property
    def is_terminal(self) -> bool:
        return states.ROLLOUT.is_terminal(self.status)

    def transition_to(
        self, status: RolloutStatus, now: datetime, reason: str | None = None
    ) -> Self:
        states.ROLLOUT.ensure(self.status, status)
        return replace(
            self,
            status=status,
            status_reason=reason if reason is not None else self.status_reason,
            finished_at=now if states.ROLLOUT.is_terminal(status) else self.finished_at,
            updated_at=now,
        )

    def at_step(self, index: int, now: datetime) -> Self:
        return replace(self, current_step=index, step_started_at=None, updated_at=now)

    def observing_since(self, now: datetime) -> Self:
        return replace(self, step_started_at=now, updated_at=now)

    def with_abort_requested(self, now: datetime) -> Self:
        return replace(self, abort_requested=True, updated_at=now)
