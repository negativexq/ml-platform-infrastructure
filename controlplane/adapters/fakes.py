"""In-memory providers. They make every use case testable with no MLflow, Argo or
Kubernetes present, and double as executable documentation of each port's contract."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from uuid import UUID

from controlplane.application.providers import (
    ExperimentRun,
    ExternalState,
    RevisionMetrics,
    ServingSpec,
    ServingState,
    ServingStatus,
    WorkflowSpec,
    WorkflowStatus,
)
from controlplane.domain.errors import NotFound

_TERMINAL = {ExternalState.SUCCEEDED, ExternalState.FAILED, ExternalState.CANCELLED}


class FakeExperimentProvider:
    def __init__(self) -> None:
        self.runs: dict[str, ExperimentRun] = {}
        self._aliases: dict[tuple[str, str], str] = {}

    def ensure_experiment(self, project_id: UUID, name: str) -> str:
        return f"exp-{project_id.hex[:12]}-{name}"  # deterministic per project

    def get_run(self, ref: str) -> ExperimentRun:
        try:
            return self.runs[ref]
        except KeyError:
            raise NotFound("experiment run", ref) from None

    def set_model_alias(self, model: str, alias: str, version_ref: str) -> None:
        self._aliases[(model, alias)] = version_ref

    def get_model_alias(self, model: str, alias: str) -> str | None:
        return self._aliases.get((model, alias))


class FakeWorkflowProvider:
    def __init__(self) -> None:
        self.submitted: dict[str, WorkflowSpec] = {}
        self._by_key: dict[str, str] = {}
        self._status: dict[str, WorkflowStatus] = {}
        self._logs: dict[tuple[str, str], str] = {}

    def submit(self, spec: WorkflowSpec, idempotency_key: str) -> str:
        if idempotency_key in self._by_key:
            return self._by_key[idempotency_key]
        ref = "wf-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:12]
        self._by_key[idempotency_key] = ref
        self.submitted[ref] = spec
        self._status[ref] = WorkflowStatus(ExternalState.PENDING)
        return ref

    def get_status(self, ref: str) -> WorkflowStatus:
        try:
            return self._status[ref]
        except KeyError:
            raise NotFound("workflow", ref) from None

    def cancel(self, ref: str) -> None:
        current = self.get_status(ref)
        if current.state not in _TERMINAL:
            self._status[ref] = WorkflowStatus(ExternalState.CANCELLED, current.steps)

    def get_logs(self, ref: str, step: str) -> str:
        self.get_status(ref)
        return self._logs.get((ref, step), "")

    # test controls
    def set_state(self, ref: str, state: ExternalState, *, reason: str | None = None) -> None:
        self._status[ref] = WorkflowStatus(state, reason=reason)

    def set_logs(self, ref: str, step: str, text: str) -> None:
        self._logs[(ref, step)] = text


class FakeServingProvider:
    def __init__(self) -> None:
        self.specs: dict[str, ServingSpec] = {}
        self.traffic: dict[str, Mapping[int, int]] = {}

    def deploy(self, spec: ServingSpec) -> str:
        ref = f"{spec.namespace}/{spec.name}"
        self.specs[ref] = spec
        return ref

    def get_status(self, ref: str) -> ServingStatus:
        spec = self.specs.get(ref)
        if spec is None:
            return ServingStatus(ServingState.ABSENT)
        return ServingStatus(
            ServingState.READY, ready_revisions=(spec.revision,), url=f"http://{spec.name}.local"
        )

    def set_traffic(self, ref: str, split: Mapping[int, int]) -> None:
        if sum(split.values()) != 100:
            raise ValueError("traffic weights must sum to 100")
        self.traffic[ref] = dict(split)

    def delete(self, ref: str) -> None:
        self.specs.pop(ref, None)
        self.traffic.pop(ref, None)


class FakeMetricsProvider:
    def __init__(self) -> None:
        self.by_revision: dict[tuple[str, int], RevisionMetrics] = {}

    def revision_metrics(self, endpoint_ref: str, revision: int) -> RevisionMetrics:
        return self.by_revision.get((endpoint_ref, revision), RevisionMetrics(None, None, None))


class FakeArtifactProvider:
    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def exists(self, uri: str) -> bool:
        return uri in self._blobs

    def read(self, uri: str) -> bytes:
        try:
            return self._blobs[uri]
        except KeyError:
            raise NotFound("artifact", uri) from None

    def write(self, uri: str, data: bytes) -> None:
        self._blobs[uri] = data
