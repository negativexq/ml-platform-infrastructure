"""In-memory providers. They make every use case testable with no MLflow, Argo or
Kubernetes present, and double as executable documentation of each port's contract."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from uuid import UUID

from controlplane.application.providers import (
    ExperimentRun,
    ExternalState,
    NamespaceSpec,
    NamespaceState,
    Observation,
    RevisionMetrics,
    ServingSpec,
    ServingState,
    ServingStatus,
    WorkflowSpec,
    WorkflowStatus,
)
from controlplane.domain.errors import Conflict, NotFound

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

    def find_runs(self, experiment_ref: str, tags: Mapping[str, str]) -> Sequence[ExperimentRun]:
        return [
            run
            for ref, run in self.runs.items()
            if ref.startswith(f"{experiment_ref}/")
            and all(run.tags.get(k) == v for k, v in tags.items())
        ]

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
    def set_state(
        self,
        ref: str,
        state: ExternalState,
        *,
        reason: str | None = None,
        exit_code: int | None = None,
    ) -> None:
        exit_codes = {} if exit_code is None else {"main": exit_code}
        self._status[ref] = WorkflowStatus(state, reason=reason, exit_codes=exit_codes)

    def set_steps(
        self,
        ref: str,
        steps: Mapping[str, ExternalState],
        state: ExternalState,
        *,
        reason: str | None = None,
        exit_codes: Mapping[str, int] | None = None,
    ) -> None:
        """Report per-step states plus the overall workflow state, as Argo does for a DAG."""
        self._status[ref] = WorkflowStatus(
            state, steps=dict(steps), reason=reason, exit_codes=dict(exit_codes or {})
        )

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


_NAMESPACED = (
    "namespace",
    "serviceaccount",
    "resourcequota",
    "limitrange",
    "networkpolicy",
    "role",
    "rolebinding",
)


class FakeClusterProvider:
    """A cluster that is just a set of (namespace -> resources) with the same
    ownership and idempotency rules as the real adapter."""

    def __init__(self) -> None:
        self.namespaces: dict[str, set[str]] = {}
        self.owners: dict[str, UUID] = {}
        self.terminating: set[str] = set()
        self.apply_calls = 0
        self.mutations = 0  # resources actually created/changed; stays flat when converged
        self.fail_apply: Exception | None = None
        self.silently_incomplete = False  # apply "succeeds" but creates nothing

    def observe(self, spec: NamespaceSpec) -> Observation:
        if spec.namespace in self.terminating:
            return Observation(NamespaceState.TERMINATING)
        if spec.namespace not in self.namespaces:
            return Observation(NamespaceState.ABSENT)
        missing = tuple(r for r in _NAMESPACED if r not in self.namespaces[spec.namespace])
        return Observation(NamespaceState.PRESENT, missing)

    def apply(self, spec: NamespaceSpec) -> tuple[str, ...]:
        self.apply_calls += 1
        if self.fail_apply is not None:
            raise self.fail_apply
        if self.silently_incomplete:
            return ()
        owner = self.owners.get(spec.namespace)
        if owner is not None and owner != spec.project_id:
            raise Conflict(f"namespace {spec.namespace!r} belongs to another project")
        have = self.namespaces.setdefault(spec.namespace, set())
        self.owners[spec.namespace] = spec.project_id
        changed = tuple(r for r in _NAMESPACED if r not in have)
        have.update(changed)
        self.mutations += len(changed)
        return changed

    def delete(self, namespace: str, project_id: UUID) -> None:
        owner = self.owners.get(namespace)
        if namespace in self.namespaces and owner != project_id:
            raise Conflict(f"namespace {namespace!r} is not owned by project {project_id}")
        self.namespaces.pop(namespace, None)
        self.owners.pop(namespace, None)
        self.terminating.discard(namespace)

    # test controls
    def remove_namespace(self, namespace: str) -> None:
        self.namespaces.pop(namespace, None)
        self.owners.pop(namespace, None)

    def remove_resource(self, namespace: str, resource: str) -> None:
        self.namespaces[namespace].discard(resource)
