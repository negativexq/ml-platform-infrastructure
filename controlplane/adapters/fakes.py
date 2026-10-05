"""In-memory providers. They make every use case testable with no MLflow, Argo or
Kubernetes present, and double as executable documentation of each port's contract."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from controlplane.application.providers import (
    EndpointUsageSeries,
    ExperimentRun,
    ExternalState,
    MetricsPoint,
    NamespaceSpec,
    NamespaceState,
    Observation,
    PlatformSignal,
    RegisteredVersion,
    RevisionMetrics,
    Sample,
    ServingSpec,
    ServingState,
    ServingStatus,
    UsagePoint,
    WorkflowSpec,
    WorkflowStatus,
)
from controlplane.domain.errors import Conflict, NotFound

_TERMINAL = {ExternalState.SUCCEEDED, ExternalState.FAILED, ExternalState.CANCELLED}


class FakeExperimentProvider:
    def __init__(self) -> None:
        self.runs: dict[str, ExperimentRun] = {}
        self.registered: dict[str, list[RegisteredVersion]] = {}
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

    def list_model_versions(self, model: str) -> Sequence[RegisteredVersion]:
        return list(self.registered.get(model, []))

    def model_artifact_uri(self, model: str, version_ref: str) -> str | None:
        known = any(v.ref == version_ref for v in self.registered.get(model, []))
        return f"s3://models/{model}/{version_ref}" if known else None

    def set_model_alias(self, model: str, alias: str, version_ref: str) -> None:
        self._aliases[(model, alias)] = version_ref

    def delete_model_alias(self, model: str, alias: str) -> None:
        self._aliases.pop((model, alias), None)

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

    def delete(self, ref: str) -> None:
        self.submitted.pop(ref, None)
        self._status.pop(ref, None)
        self._logs = {k: v for k, v in self._logs.items() if k[0] != ref}

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
    """Serving resources as a dict. `auto_ready=False` holds a new revision PENDING
    until a test calls `mark_ready`. Canary semantics follow KServe: a spec with
    `canary_percent` makes the new revision take that share and the previously
    serving revision keep the rest."""

    def __init__(self) -> None:
        self.specs: dict[str, ServingSpec] = {}
        self.previous: dict[str, int] = {}
        self.traffic: dict[str, Mapping[int, int]] = {}
        self.auto_ready = True
        self.deploy_calls = 0
        self.failed: dict[str, str] = {}
        self._ready: set[str] = set()
        self.requests: list[tuple[str, Mapping[str, Any]]] = []
        self.history: list[tuple[int, int | None]] = []  # (revision, canary_percent) per deploy

    def deploy(self, spec: ServingSpec) -> str:
        self.deploy_calls += 1
        ref = f"{spec.namespace}/{spec.name}"
        old = self.specs.get(ref)
        if old is not None and old.revision != spec.revision:
            self.previous[ref] = old.revision
        revision_changed = old is None or old.revision != spec.revision
        self.specs[ref] = spec
        self.history.append((spec.revision, spec.canary_percent))
        self.failed.pop(ref, None)
        if revision_changed:
            self._ready.discard(ref)
            if self.auto_ready:
                self._ready.add(ref)
        return ref

    def matches(self, spec: ServingSpec) -> bool:
        return self.specs.get(f"{spec.namespace}/{spec.name}") == spec

    def split(self, ref: str) -> dict[int, int]:
        """Who gets what share of traffic right now."""
        spec = self.specs.get(ref)
        if spec is None:
            return {}
        percent = spec.canary_percent
        previous = self.previous.get(ref)
        if percent is None or percent >= 100 or previous is None:
            return {spec.revision: 100}
        shares = {spec.revision: percent}
        if percent < 100:
            shares[previous] = 100 - percent
        return {rev: pct for rev, pct in shares.items() if pct > 0}

    def get_status(self, ref: str) -> ServingStatus:
        spec = self.specs.get(ref)
        if spec is None:
            return ServingStatus(ServingState.ABSENT)
        if ref in self.failed:
            return ServingStatus(ServingState.FAILED, spec.revision, reason=self.failed[ref])
        if ref in self._ready:
            ready = {spec.revision}
            previous = self.previous.get(ref)
            names = {spec.revision: f"{spec.name}-r{spec.revision}"}
            if previous is not None:
                ready.add(previous)
                names[previous] = f"{spec.name}-r{previous}"
            return ServingStatus(
                ServingState.READY,
                spec.revision,
                ready_revisions=tuple(sorted(ready)),
                url=f"http://{spec.name}.{spec.namespace}.svc",
                backend_revisions=names,
            )
        return ServingStatus(ServingState.PENDING, spec.revision)

    def set_traffic(self, ref: str, split: Mapping[int, int]) -> None:
        if sum(split.values()) != 100:
            raise ValueError("traffic weights must sum to 100")
        self.traffic[ref] = dict(split)

    def predict(self, ref: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if ref not in self._ready:
            raise ConnectionError(f"{ref} is not serving")
        self.requests.append((ref, payload))
        return {"predictions": [0.0 for _ in payload.get("instances", [])]}

    def chat(self, ref: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """A canned but well-formed chat completion, with token counts (words, roughly)."""
        if ref not in self._ready:
            raise ConnectionError(f"{ref} is not serving")
        self.requests.append((ref, payload))
        messages = payload.get("messages") or []
        asked = str(messages[-1].get("content", "")) if messages else ""
        answer = (
            f"(demo model) You asked: {asked[:120]!r}. A real deployment answers with the "
            "served LLM; this fake answers the same way every time."
        )
        prompt = sum(len(str(m.get("content", "")).split()) + 4 for m in messages)
        completion = len(answer.split())
        return {
            "id": f"chatcmpl-{len(self.requests)}",
            "object": "chat.completion",
            "model": ref.partition("/")[2],
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": answer},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            },
        }

    def invoke(self, ref: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """A function that echoes what it was sent, saying which function answered."""
        if ref not in self._ready:
            raise ConnectionError(f"{ref} is not serving")
        self.requests.append((ref, payload))
        return {"function": ref.partition("/")[2], "received": dict(payload), "ok": True}

    def delete(self, ref: str) -> None:
        self.specs.pop(ref, None)
        self.previous.pop(ref, None)
        self.traffic.pop(ref, None)
        self._ready.discard(ref)

    # test controls
    def mark_ready(self, ref: str) -> None:
        self._ready.add(ref)

    def mark_failed(self, ref: str, reason: str) -> None:
        self.failed[ref] = reason


class FakeMetricsProvider:
    def __init__(self) -> None:
        self.by_revision: dict[tuple[str, int], RevisionMetrics] = {}
        # raw samples per (endpoint ref, revision); revision_history averages them per step
        self.history: dict[tuple[str, int], list[MetricsPoint]] = {}
        # or a function of time, sampled at query time, for a series that keeps going (demo)
        self.series: dict[tuple[str, int], Callable[[datetime], MetricsPoint | None]] = {}

    def revision_metrics(
        self, endpoint_ref: str, revision: int, backend_revision: str | None = None
    ) -> RevisionMetrics:
        return self.by_revision.get((endpoint_ref, revision), RevisionMetrics(None, None, None))

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
        live = self.series.get((endpoint_ref, revision))
        if live is not None:
            total = int((end - start).total_seconds())
            moments = (
                start + timedelta(seconds=s) for s in range(step_seconds, total + 1, step_seconds)
            )
            return [p for p in map(live, moments) if p is not None]
        samples = [
            p for p in self.history.get((endpoint_ref, revision), []) if start <= p.at <= end
        ]
        buckets: dict[int, list[MetricsPoint]] = {}
        for p in samples:
            buckets.setdefault(int((p.at - start).total_seconds() // step_seconds), []).append(p)

        def mean(values: list[float | None]) -> float | None:
            present = [v for v in values if v is not None]
            return sum(present) / len(present) if present else None

        return [
            MetricsPoint(
                at=start + timedelta(seconds=(i + 1) * step_seconds),
                p95_latency_ms=mean([p.p95_latency_ms for p in group]),
                error_rate=mean([p.error_rate for p in group]),
                requests_per_second=mean([p.requests_per_second for p in group]),
            )
            for i, group in sorted(buckets.items())
        ]


class FakePlatformTelemetry:
    """Series as functions of time, sampled once per step: (signal, group) -> value or None."""

    def __init__(self) -> None:
        self.series: dict[tuple[PlatformSignal, str], Callable[[datetime], float | None]] = {}
        self.fail: str | None = None  # set to make every query raise ConnectionError

    def platform_series(
        self, signal: PlatformSignal, *, start: datetime, end: datetime, step_seconds: int
    ) -> Mapping[str, Sequence[Sample]]:
        if self.fail:
            raise ConnectionError(self.fail)
        total = int((end - start).total_seconds())
        moments = [
            start + timedelta(seconds=s) for s in range(step_seconds, total + 1, step_seconds)
        ]
        out: dict[str, Sequence[Sample]] = {}
        for (sig, group), value_at in sorted(self.series.items()):
            if sig is not signal:
                continue
            points = [Sample(at, v) for at in moments if (v := value_at(at)) is not None]
            if points:
                out[group] = points
        return out


class FakeUsage:
    """Gateway usage in memory. Records calls like the gateway's metrics do (so a demo
    gateway's calls show up in the usage panel) and can be seeded with per-minute series."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.series: dict[
            tuple[str, str, str], Callable[[datetime], tuple[float, float, float] | None]
        ] = {}  # (project, endpoint, caller) -> (units, rejected, errors) per minute
        self.p95: dict[tuple[str, str], Callable[[datetime], float | None]] = {}
        # LLM callers seeded above: the share of their units that were prompt tokens
        self.token_split: dict[tuple[str, str, str], float] = {}
        self.calls: list[tuple[datetime, Any]] = []
        self._clock = clock

    def record(self, call: Any) -> None:  # a gateway CallRecord
        if self._clock is not None:
            self.calls.append((self._clock(), call))

    def endpoint_usage(
        self, project: str, endpoint: str, *, start: datetime, end: datetime, step_seconds: int
    ) -> EndpointUsageSeries:
        total = int((end - start).total_seconds())
        steps = range(step_seconds, total + 1, step_seconds)
        moments = [start + timedelta(seconds=s) for s in steps]
        rates: dict[str, dict[datetime, list[float]]] = {}
        for (p, e, caller), value_at in sorted(self.series.items()):
            if (p, e) == (project, endpoint):
                for at in moments:
                    if (value := value_at(at)) is not None:
                        rates.setdefault(caller, {})[at] = list(value)
        per_minute = 60 / step_seconds
        for at, call in self.calls:
            if (call.project, call.endpoint) != (project, endpoint) or not start < at <= end:
                continue
            moment = moments[
                min(len(moments) - 1, int((at - start).total_seconds()) // step_seconds)
            ]
            rate = rates.setdefault(call.caller, {}).setdefault(moment, [0.0, 0.0, 0.0])
            rate[0] += call.units * per_minute
            rate[1] += per_minute if 400 <= call.status < 500 else 0
            rate[2] += per_minute if call.status >= 500 else 0
        callers = {
            caller: [UsagePoint(at, *by_at[at]) for at in sorted(by_at)]
            for caller, by_at in sorted(rates.items())
        }
        tokens: dict[str, tuple[float, float]] = {}
        for (p_, e_, who), ratio in self.token_split.items():
            if (p_, e_) == (project, endpoint) and who in callers:
                used = sum(pt.units for pt in callers[who]) * step_seconds / 60
                tokens[who] = (used * ratio, used * (1 - ratio))
        for at, call in self.calls:
            here = (call.project, call.endpoint) == (project, endpoint) and start < at <= end
            if here and (call.prompt_tokens or call.completion_tokens):
                sent, made = tokens.get(call.caller, (0.0, 0.0))
                tokens[call.caller] = (sent + call.prompt_tokens, made + call.completion_tokens)
        latency_at = self.p95.get((project, endpoint))
        p95 = [
            Sample(at, ms) for at in moments if latency_at and (ms := latency_at(at)) is not None
        ]
        return EndpointUsageSeries(callers=callers, p95_latency_ms=p95, tokens=tokens)


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


class FakeSecretProvider:
    """Test/demo backend; production always explicitly configures Kubernetes storage."""

    def __init__(self) -> None:
        self._data: dict[tuple[UUID, str], tuple[Any, dict[str, str]]] = {}
        self._version = 0

    def list(self, project: Any) -> list[Any]:
        return sorted(
            [v[0] for (pid, _), v in self._data.items() if pid == project.id], key=lambda i: i.name
        )

    def get(self, project: Any, name: str) -> Any:
        entry = self._data.get((project.id, name))
        if entry is None:
            raise NotFound("project secret", name)
        return entry[0]

    def put(
        self,
        project: Any,
        name: str,
        values: Mapping[str, str],
        kind: str,
        expected_version: str | None,
        annotations: Mapping[str, str] | None = None,
    ) -> Any:
        from controlplane.application.secrets import SecretInfo
        from controlplane.domain.errors import AlreadyExists, Conflict

        existing = self._data.get((project.id, name))
        if expected_version is None and existing is not None:
            raise AlreadyExists("project secret", name)
        if expected_version is not None and (
            existing is None or existing[0].version != expected_version
        ):
            raise Conflict("secret changed")
        self._version += 1
        info = SecretInfo(
            name,
            tuple(sorted(values)),
            kind,
            str(self._version),
            dict(
                annotations
                if annotations is not None
                else existing[0].annotations
                if existing
                else {}
            ),
        )
        self._data[(project.id, name)] = (info, dict(values))
        return info

    def delete(self, project: Any, name: str, expected_version: str) -> None:
        from controlplane.domain.errors import Conflict

        if self.get(project, name).version != expected_version:
            raise Conflict("secret changed")
        del self._data[(project.id, name)]
