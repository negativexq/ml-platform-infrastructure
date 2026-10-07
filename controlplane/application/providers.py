"""Outbound ports to the systems the platform adapts.

MLflow, Argo and KServe are implementation details behind these interfaces.
Nothing here names them; adapters under `controlplane.adapters` implement them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from controlplane.domain.secrets import SecretRefs


class ExternalState(StrEnum):
    """What a workflow system can tell us. Mapped to platform states by the caller."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"  # a step that never ran because an upstream step did not succeed


# --- experiment tracking ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExperimentRun:
    ref: str
    params: Mapping[str, str] = field(default_factory=dict)
    metrics: Mapping[str, float] = field(default_factory=dict)
    tags: Mapping[str, str] = field(default_factory=dict)
    artifact_uri: str | None = None


@dataclass(frozen=True, slots=True)
class RegisteredVersion:
    ref: str  # the registry's version number
    run_ref: str | None  # the tracking run that produced it


@runtime_checkable
class ExperimentProvider(Protocol):
    def ensure_experiment(self, project_id: UUID, name: str) -> str:
        """Create-or-get the experiment for a project. Deterministic for a given project."""

    def get_run(self, ref: str) -> ExperimentRun: ...

    def find_runs(self, experiment_ref: str, tags: Mapping[str, str]) -> Sequence[ExperimentRun]:
        """Runs in the experiment carrying every one of `tags`. This is how a
        platform run is joined to its tracked runs without storing tracker ids."""

    def model_artifact_uri(self, model: str, version_ref: str) -> str | None:
        """Where a serving runtime can load this registry version from."""

    def list_model_versions(self, model: str) -> Sequence[RegisteredVersion]:
        """Versions registered under this name; empty if the name is unknown."""

    def set_model_alias(self, model: str, alias: str, version_ref: str) -> None: ...

    def delete_model_alias(self, model: str, alias: str) -> None: ...

    def get_model_alias(self, model: str, alias: str) -> str | None: ...


# --- workflow execution ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepSpec:
    name: str
    image: str
    command: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    resources: Mapping[str, str] = field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    secret_refs: SecretRefs = field(default_factory=SecretRefs)


@dataclass(frozen=True, slots=True)
class WorkflowSpec:
    name: str
    namespace: str
    steps: tuple[StepSpec, ...]
    labels: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: int = 3600
    image_pull_secrets: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkflowStatus:
    state: ExternalState
    steps: Mapping[str, ExternalState] = field(default_factory=dict)
    reason: str | None = None
    exit_codes: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LogTarget:
    namespace: str
    pod: str
    pod_uid: str
    workflow: str
    workflow_uid: str


@runtime_checkable
class WorkflowProvider(Protocol):
    def submit(self, spec: WorkflowSpec, idempotency_key: str) -> str:
        """Submit a workflow and return its external reference.

        The same `idempotency_key` must never create a second workload.
        """

    def get_status(self, ref: str) -> WorkflowStatus: ...

    def cancel(self, ref: str) -> None: ...

    def delete(self, ref: str) -> None:
        """Idempotently delete a workflow and its owned pods after retention expires."""

    def get_logs(self, ref: str, step: str) -> str: ...

    def get_log_target(self, ref: str, step: str) -> LogTarget | None: ...


# --- serving ---------------------------------------------------------------


class ServingState(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"
    ABSENT = "ABSENT"


@dataclass(frozen=True, slots=True)
class ServingSpec:
    name: str
    namespace: str
    model_uri: str
    revision: int
    labels: Mapping[str, str] = field(default_factory=dict)
    # None: this revision takes all traffic. Otherwise it takes this share and the
    # previously serving revision keeps the rest (a canary).
    canary_percent: int | None = None
    # How it is served: "mlflow" (v2 protocol) or "huggingface" (an LLM runtime answering
    # OpenAI-compatible chat completions), with GPUs and a context window for LLMs.
    runtime: str = "mlflow"
    gpus: int = 0
    context_length: int | None = None
    min_scale: int = 1
    max_scale: int = 1
    # A function ("container" runtime): replicas, concurrency, port and environment.
    function: Mapping[str, Any] | None = None
    secret_refs: SecretRefs = field(default_factory=SecretRefs)


@dataclass(frozen=True, slots=True)
class ServingStatus:
    state: ServingState
    deployed_revision: int | None = None  # the revision the resource is configured with
    ready_revisions: tuple[int, ...] = ()  # revisions whose model has loaded and is serving
    # platform revision -> the serving system's own identifier for it (used to label metrics)
    backend_revisions: Mapping[int, str] = field(default_factory=dict)
    url: str | None = None
    reason: str | None = None


@runtime_checkable
class ServingProvider(Protocol):
    def deploy(self, spec: ServingSpec) -> str:
        """Create-or-update the serving resource and return its external reference."""

    def get_status(self, ref: str) -> ServingStatus: ...

    def matches(self, spec: ServingSpec) -> bool:
        """Whether the observed owned serving configuration matches the immutable intent."""

    def set_traffic(self, ref: str, split: Mapping[int, int]) -> None:
        """Weights by revision, summing to 100."""

    def predict(self, ref: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Send an inference request to the live endpoint and return its response."""

    def chat(self, ref: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """One OpenAI-style chat completion from a live LLM endpoint (not streamed)."""

    def invoke(self, ref: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Call a live function with any JSON; it answers any JSON."""

    def delete(self, ref: str) -> None: ...


# --- cluster (project isolation) --------------------------------------------


class NamespaceState(StrEnum):
    ABSENT = "ABSENT"
    TERMINATING = "TERMINATING"
    PRESENT = "PRESENT"


@dataclass(frozen=True, slots=True)
class NamespaceSpec:
    """Everything a project needs in the cluster, derived from the project alone."""

    project_id: UUID
    project_name: str
    namespace: str
    labels: Mapping[str, str]
    quota: Mapping[str, str]
    default_limits: Mapping[str, str]
    default_requests: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class Observation:
    state: NamespaceState
    # Resources that are missing or differ from the spec ("namespace", "resourcequota", ...)
    drifted: tuple[str, ...] = ()


@runtime_checkable
class ClusterProvider(Protocol):
    def observe(self, spec: NamespaceSpec) -> Observation: ...

    def apply(self, spec: NamespaceSpec) -> tuple[str, ...]:
        """Converge the cluster on the spec. Returns the resources it had to
        create or change; an already-converged cluster returns ()."""

    def delete(self, namespace: str, project_id: UUID) -> None:
        """Delete the namespace only if this project owns it (Conflict otherwise)."""


# --- metrics & artifacts ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class RevisionMetrics:
    p95_latency_ms: float | None
    error_rate: float | None  # 0..1
    requests_per_second: float | None
    requests: float | None = None  # requests observed in the window


@dataclass(frozen=True, slots=True)
class MetricsPoint:
    """One revision's serving metrics at one moment (each over the provider's rate window)."""

    at: datetime
    p95_latency_ms: float | None
    error_rate: float | None  # 0..1
    requests_per_second: float | None


@runtime_checkable
class MetricsProvider(Protocol):
    def revision_metrics(
        self, endpoint_ref: str, revision: int, backend_revision: str | None = None
    ) -> RevisionMetrics:
        """Metrics of one revision only, so a canary can be judged apart from stable."""

    def revision_history(
        self,
        endpoint_ref: str,
        revision: int,
        backend_revision: str | None,
        *,
        start: datetime,
        end: datetime,
        step_seconds: int,
    ) -> Sequence[MetricsPoint]:
        """The same metrics over time, oldest first, about one point per step. Moments with
        no traffic have no point (or None values): a gap, never a made-up zero."""


class PlatformSignal(StrEnum):
    """The control plane's own health, as its telemetry backend records it."""

    API_REQUESTS = "api_requests"  # requests per second
    API_ERRORS = "api_errors"  # share of 5xx responses, 0..1
    API_LATENCY = "api_latency"  # p95, milliseconds
    RECONCILE_PASSES = "reconcile_passes"  # completed passes per minute, by reconciler
    RECONCILE_ERRORS = "reconcile_errors"  # share of reconciliations that errored, by reconciler
    PROVIDER_ERRORS = "provider_errors"  # share of external calls that errored, by system
    PROVIDER_LATENCY = "provider_latency"  # p95 of external calls in ms, by system
    TRANSITIONS = "transitions"  # state changes per minute, by entity type
    GATEWAY_REQUESTS = "gateway_requests"  # public calls per second
    GATEWAY_ERRORS = "gateway_errors"  # share of public calls that failed (5xx), 0..1
    GATEWAY_LATENCY = "gateway_latency"  # p95 of a public call in ms, model time included
    GATEWAY_TOKENS = "gateway_tokens"  # LLM tokens per minute, by direction (prompt, completion)


@dataclass(frozen=True, slots=True)
class Sample:
    at: datetime
    value: float


@runtime_checkable
class PlatformTelemetry(Protocol):
    def platform_series(
        self, signal: PlatformSignal, *, start: datetime, end: datetime, step_seconds: int
    ) -> Mapping[str, Sequence[Sample]]:
        """One series per group (a reconciler, an external system...), keyed by the group's
        name, or by "" for a signal with no groups. Oldest first; no data is a gap."""


@dataclass(frozen=True, slots=True)
class UsagePoint:
    """One caller's use of one endpoint at one moment, each per minute."""

    at: datetime
    units: float  # what quotas count: requests for a model
    rejected: float  # refused by the gateway (4xx: limits, keys, body size)
    errors: float  # failed upstream (5xx)


@dataclass(frozen=True, slots=True)
class EndpointUsageSeries:
    callers: Mapping[str, Sequence[UsagePoint]]  # caller (a key's name) -> points, oldest first
    p95_latency_ms: Sequence[Sample]  # all callers together
    # LLMs: tokens in the window by caller, (prompt, completion)
    tokens: Mapping[str, tuple[float, float]] = field(default_factory=dict)


@runtime_checkable
class UsageProvider(Protocol):
    def endpoint_usage(
        self, project: str, endpoint: str, *, start: datetime, end: datetime, step_seconds: int
    ) -> EndpointUsageSeries:
        """Public calls to one endpoint through the gateway, by caller."""


@runtime_checkable
class ArtifactProvider(Protocol):
    def exists(self, uri: str) -> bool: ...

    def read(self, uri: str) -> bytes: ...

    def write(self, uri: str, data: bytes) -> None: ...
