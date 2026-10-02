"""Outbound ports to the systems the platform adapts.

MLflow, Argo and KServe are implementation details behind these interfaces.
Nothing here names them; adapters under `controlplane.adapters` implement them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID


class ExternalState(StrEnum):
    """What a workflow system can tell us. Mapped to platform states by the caller."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# --- experiment tracking ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExperimentRun:
    ref: str
    params: Mapping[str, str] = field(default_factory=dict)
    metrics: Mapping[str, float] = field(default_factory=dict)
    tags: Mapping[str, str] = field(default_factory=dict)
    artifact_uri: str | None = None


@runtime_checkable
class ExperimentProvider(Protocol):
    def ensure_experiment(self, project_id: UUID, name: str) -> str:
        """Create-or-get the experiment for a project. Deterministic for a given project."""

    def get_run(self, ref: str) -> ExperimentRun: ...

    def set_model_alias(self, model: str, alias: str, version_ref: str) -> None: ...

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


@dataclass(frozen=True, slots=True)
class WorkflowSpec:
    name: str
    namespace: str
    steps: tuple[StepSpec, ...]
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkflowStatus:
    state: ExternalState
    steps: Mapping[str, ExternalState] = field(default_factory=dict)
    reason: str | None = None
    exit_codes: Mapping[str, int] = field(default_factory=dict)


@runtime_checkable
class WorkflowProvider(Protocol):
    def submit(self, spec: WorkflowSpec, idempotency_key: str) -> str:
        """Submit a workflow and return its external reference.

        The same `idempotency_key` must never create a second workload.
        """

    def get_status(self, ref: str) -> WorkflowStatus: ...

    def cancel(self, ref: str) -> None: ...

    def get_logs(self, ref: str, step: str) -> str: ...


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


@dataclass(frozen=True, slots=True)
class ServingStatus:
    state: ServingState
    ready_revisions: tuple[int, ...] = ()
    url: str | None = None
    reason: str | None = None


@runtime_checkable
class ServingProvider(Protocol):
    def deploy(self, spec: ServingSpec) -> str:
        """Create-or-update the serving resource and return its external reference."""

    def get_status(self, ref: str) -> ServingStatus: ...

    def set_traffic(self, ref: str, split: Mapping[int, int]) -> None:
        """Weights by revision, summing to 100."""

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


@runtime_checkable
class MetricsProvider(Protocol):
    def revision_metrics(self, endpoint_ref: str, revision: int) -> RevisionMetrics: ...


@runtime_checkable
class ArtifactProvider(Protocol):
    def exists(self, uri: str) -> bool: ...

    def read(self, uri: str) -> bytes: ...

    def write(self, uri: str, data: bytes) -> None: ...
