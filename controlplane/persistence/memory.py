"""In-memory unit of work: the fake that lets every unit test run without a database.

Reads and writes go to a private copy that replaces the shared store only on
commit, so rollback semantics match the SQL implementation.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import TracebackType
from typing import Self
from uuid import UUID

from controlplane.domain.access import Membership
from controlplane.domain.api_keys import ApiKey
from controlplane.domain.audit import AuditEvent
from controlplane.domain.data import DataConnection, DatasetVersion
from controlplane.domain.entities import (
    Deployment,
    DeploymentRevision,
    Endpoint,
    EndpointLimits,
    Evaluation,
    JobDefinition,
    Model,
    ModelVersion,
    PipelineDefinition,
    PipelineRun,
    Project,
    Promotion,
    Rollout,
    Run,
    StepRun,
)
from controlplane.domain.errors import AlreadyExists, Conflict, NotFound
from controlplane.domain.model_monitoring import MonitoringReport
from controlplane.domain.schedules import Schedule, ScheduleExecution
from controlplane.domain.states import (
    DeploymentStatus,
    EndpointStatus,
    EvaluationStatus,
    ModelStatus,
    ProjectStatus,
    RolloutStatus,
    RunStatus,
    StepStatus,
)


@dataclass
class MemoryStore:
    monitoring_reports: dict[UUID, MonitoringReport] = field(default_factory=dict)
    data_connections: dict[UUID, DataConnection] = field(default_factory=dict)
    dataset_versions: dict[UUID, DatasetVersion] = field(default_factory=dict)
    schedules: dict[UUID, Schedule] = field(default_factory=dict)
    schedule_executions: dict[UUID, ScheduleExecution] = field(default_factory=dict)
    notification_reads: dict[tuple[str, str], datetime] = field(default_factory=dict)
    projects: dict[UUID, Project] = field(default_factory=dict)
    jobs: dict[UUID, JobDefinition] = field(default_factory=dict)
    runs: dict[UUID, Run] = field(default_factory=dict)
    pipelines: dict[UUID, PipelineDefinition] = field(default_factory=dict)
    pipeline_runs: dict[UUID, PipelineRun] = field(default_factory=dict)
    step_runs: dict[UUID, StepRun] = field(default_factory=dict)
    models: dict[UUID, Model] = field(default_factory=dict)
    model_versions: dict[UUID, ModelVersion] = field(default_factory=dict)
    evaluations: dict[UUID, Evaluation] = field(default_factory=dict)
    promotions: dict[UUID, Promotion] = field(default_factory=dict)
    deployments: dict[UUID, Deployment] = field(default_factory=dict)
    revisions: dict[UUID, DeploymentRevision] = field(default_factory=dict)
    endpoints: dict[UUID, Endpoint] = field(default_factory=dict)
    rollouts: dict[UUID, Rollout] = field(default_factory=dict)
    audit: list[AuditEvent] = field(default_factory=list)
    memberships: dict[UUID, Membership] = field(default_factory=dict)
    api_keys: dict[str, ApiKey] = field(default_factory=dict)


class _ApiKeys:
    def __init__(self, data: dict[str, ApiKey]) -> None:
        self._data = data

    def add(self, key: ApiKey) -> None:
        if any(k.project_id == key.project_id and k.name == key.name for k in self._data.values()):
            raise AlreadyExists("api key", key.name)
        self._data[key.key_id] = key

    def get(self, key_id: str) -> ApiKey | None:
        return self._data.get(key_id)

    def list(self, project_id: UUID) -> Sequence[ApiKey]:
        mine = [k for k in self._data.values() if k.project_id == project_id]
        return sorted(mine, key=lambda k: k.created_at, reverse=True)

    def update(self, key: ApiKey) -> None:
        if key.key_id not in self._data:
            raise NotFound("api key", key.key_id)
        self._data[key.key_id] = key

    def touch(self, key_id: str, at: datetime) -> None:
        current = self._data.get(key_id)
        if current is not None:
            self._data[key_id] = current.used(at)


class _Memberships:
    def __init__(self, data: dict[UUID, Membership]) -> None:
        self._data = data

    def add(self, membership: Membership) -> None:
        if self.get(membership.project_id, membership.subject) is not None:
            raise AlreadyExists("member", membership.subject)
        self._data[membership.id] = membership

    def get(self, project_id: UUID, subject: str) -> Membership | None:
        return next(
            (m for m in self._data.values() if m.project_id == project_id and m.subject == subject),
            None,
        )

    def list(self, project_id: UUID) -> Sequence[Membership]:
        return sorted(
            (m for m in self._data.values() if m.project_id == project_id), key=lambda m: m.subject
        )

    def list_for_subjects(self, subjects: Collection[str]) -> Sequence[Membership]:
        wanted = set(subjects)
        return [m for m in self._data.values() if m.subject in wanted]

    def update(self, membership: Membership) -> None:
        if membership.id not in self._data:
            raise NotFound("member", membership.subject)
        self._data[membership.id] = membership

    def remove(self, project_id: UUID, subject: str) -> None:
        found = self.get(project_id, subject)
        if found is None:
            raise NotFound("member", subject)
        del self._data[found.id]


class _Projects:
    def __init__(self, data: dict[UUID, Project]) -> None:
        self._data = data

    def add(self, project: Project) -> None:
        if any(p.name == project.name for p in self._data.values()):
            raise AlreadyExists("project", project.name)
        self._data[project.id] = project

    def get(self, project_id: UUID) -> Project | None:
        return self._data.get(project_id)

    def lock(self, project_id: UUID) -> Project | None:
        return self.get(project_id)

    def get_by_name(self, name: str) -> Project | None:
        return next((p for p in self._data.values() if p.name == name), None)

    def list(self, *, limit: int, offset: int) -> Sequence[Project]:
        return self.list_reconcilable()[offset : offset + limit]

    def list_reconcilable(self) -> Sequence[Project]:
        live = (p for p in self._data.values() if p.status is not ProjectStatus.DELETED)
        return sorted(live, key=lambda p: (p.created_at, p.id))

    def update(self, project: Project, *, expected_status: ProjectStatus) -> None:
        current = self._data.get(project.id)
        if current is None:
            raise NotFound("project", project.id)
        if current.status is not expected_status:
            raise Conflict(
                f"project {project.id} is {current.status.value}, not {expected_status.value}"
            )
        self._data[project.id] = project


class _Jobs:
    def __init__(self, data: dict[UUID, JobDefinition]) -> None:
        self._data = data

    def add(self, job: JobDefinition) -> None:
        if self.get_by_name(job.project_id, job.name) is not None:
            raise AlreadyExists("job", job.name)
        self._data[job.id] = job

    def get(self, job_id: UUID) -> JobDefinition | None:
        return self._data.get(job_id)

    def get_by_name(self, project_id: UUID, name: str) -> JobDefinition | None:
        return next(
            (j for j in self._data.values() if j.project_id == project_id and j.name == name), None
        )

    def list(self, project_id: UUID) -> Sequence[JobDefinition]:
        return sorted(
            (j for j in self._data.values() if j.project_id == project_id),
            key=lambda j: (j.created_at, j.id),
        )


class _Runs:
    def __init__(self, data: dict[UUID, Run]) -> None:
        self._data = data

    def add(self, run: Run) -> None:
        if run.idempotency_key is not None and self.get_by_idempotency_key(
            run.project_id, run.idempotency_key
        ):
            raise AlreadyExists("run", run.idempotency_key)
        self._data[run.id] = run

    def get(self, run_id: UUID) -> Run | None:
        return self._data.get(run_id)

    def get_by_idempotency_key(self, project_id: UUID, key: str) -> Run | None:
        return next(
            (
                r
                for r in self._data.values()
                if r.project_id == project_id and r.idempotency_key == key
            ),
            None,
        )

    def list(
        self,
        project_id: UUID,
        *,
        job_id: UUID | None,
        limit: int,
        offset: int,
        statuses: Collection[RunStatus] | None = None,
        finished_since: datetime | None = None,
    ) -> Sequence[Run]:
        rows = [
            r
            for r in self._data.values()
            if r.project_id == project_id
            and (job_id is None or r.job_definition_id == job_id)
            and (not statuses or r.status in statuses)
            and (finished_since is None or (r.finished_at or r.updated_at) >= finished_since)
        ]
        rows.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return rows[offset : offset + limit]

    def count(self, project_id: UUID) -> int:
        return sum(1 for r in self._data.values() if r.project_id == project_id)

    def list_active(self) -> Sequence[Run]:
        return sorted(
            (r for r in self._data.values() if not r.is_terminal),
            key=lambda r: (r.created_at, r.id),
        )

    def list_cleanup_candidates(self, before: datetime, limit: int) -> Sequence[Run]:
        rows = (
            r
            for r in self._data.values()
            if r.is_terminal
            and r.finished_at is not None
            and r.finished_at < before
            and r.external_ref is not None
            and r.workflow_cleaned_at is None
        )
        return sorted(rows, key=lambda r: (r.finished_at, r.id))[:limit]

    def update(self, run: Run, *, expected_status: RunStatus) -> None:
        current = self._data.get(run.id)
        if current is None:
            raise NotFound("run", run.id)
        if current.status is not expected_status:
            raise Conflict(f"run {run.id} is {current.status.value}, not {expected_status.value}")
        self._data[run.id] = run


class _Pipelines:
    def __init__(self, data: dict[UUID, PipelineDefinition]) -> None:
        self._data = data

    def add(self, definition: PipelineDefinition) -> None:
        if self.get_version(definition.project_id, definition.name, definition.version):
            raise AlreadyExists("pipeline", f"{definition.name}@{definition.version}")
        self._data[definition.id] = definition

    def get(self, definition_id: UUID) -> PipelineDefinition | None:
        return self._data.get(definition_id)

    def _named(self, project_id: UUID, name: str) -> list[PipelineDefinition]:
        return [d for d in self._data.values() if d.project_id == project_id and d.name == name]

    def get_version(
        self, project_id: UUID, name: str, version: int | None
    ) -> PipelineDefinition | None:
        candidates = self._named(project_id, name)
        if version is not None:
            return next((d for d in candidates if d.version == version), None)
        return max(candidates, key=lambda d: d.version, default=None)

    def list_latest(self, project_id: UUID) -> Sequence[PipelineDefinition]:
        names = sorted({d.name for d in self._data.values() if d.project_id == project_id})
        latest = (self.get_version(project_id, n, None) for n in names)
        return [d for d in latest if d is not None]


class _PipelineRuns:
    def __init__(
        self, data: dict[UUID, PipelineRun], pipelines: dict[UUID, PipelineDefinition]
    ) -> None:
        self._data = data
        self._pipelines = pipelines

    def add(self, run: PipelineRun) -> None:
        if run.idempotency_key is not None and self.get_by_idempotency_key(
            run.project_id, run.idempotency_key
        ):
            raise AlreadyExists("pipeline run", run.idempotency_key)
        self._data[run.id] = run

    def get(self, run_id: UUID) -> PipelineRun | None:
        return self._data.get(run_id)

    def get_by_idempotency_key(self, project_id: UUID, key: str) -> PipelineRun | None:
        return next(
            (
                r
                for r in self._data.values()
                if r.project_id == project_id and r.idempotency_key == key
            ),
            None,
        )

    def list(
        self,
        project_id: UUID,
        *,
        definition_ids: Sequence[UUID] | None,
        limit: int,
        offset: int,
        statuses: Collection[RunStatus] | None = None,
        finished_since: datetime | None = None,
    ) -> Sequence[PipelineRun]:
        rows = [
            r
            for r in self._data.values()
            if r.project_id == project_id
            and (definition_ids is None or r.pipeline_definition_id in definition_ids)
            and (not statuses or r.status in statuses)
            and (finished_since is None or (r.finished_at or r.updated_at) >= finished_since)
        ]
        rows.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return rows[offset : offset + limit]

    def count(self, project_id: UUID) -> int:
        return sum(1 for r in self._data.values() if r.project_id == project_id)

    def list_discovery_candidates(self, before: datetime, limit: int) -> Sequence[PipelineRun]:
        rows = [
            r
            for r in self._data.values()
            if r.status is RunStatus.SUCCEEDED
            and r.models_discovered_at is None
            and r.finished_at is not None
            and r.finished_at <= before
            and (r.model_discovery_checked_at is None or r.model_discovery_checked_at <= before)
        ]
        return sorted(rows, key=lambda r: (r.model_discovery_checked_at or r.finished_at, r.id))[
            :limit
        ]

    def mark_model_discovery(self, run_id: UUID, checked_at: datetime, completed: bool) -> bool:
        current = self._data.get(run_id)
        if (
            current is None
            or current.status is not RunStatus.SUCCEEDED
            or current.models_discovered_at
        ):
            return False
        self._data[run_id] = replace(
            current,
            model_discovery_checked_at=checked_at,
            models_discovered_at=checked_at if completed else None,
        )
        return True

    def list_active(self) -> Sequence[PipelineRun]:
        return sorted(
            (r for r in self._data.values() if not r.is_terminal),
            key=lambda r: (r.created_at, r.id),
        )

    def list_cleanup_candidates(self, before: datetime, limit: int) -> Sequence[PipelineRun]:
        rows = (
            r
            for r in self._data.values()
            if r.is_terminal
            and r.finished_at is not None
            and r.finished_at < before
            and r.external_ref is not None
            and r.workflow_cleaned_at is None
        )
        return sorted(rows, key=lambda r: (r.finished_at, r.id))[:limit]

    def update(self, run: PipelineRun, *, expected_status: RunStatus) -> None:
        current = self._data.get(run.id)
        if current is None:
            raise NotFound("pipeline run", run.id)
        if current.status is not expected_status:
            raise Conflict(
                f"pipeline run {run.id} is {current.status.value}, not {expected_status.value}"
            )
        self._data[run.id] = replace(
            run,
            models_discovered_at=current.models_discovered_at,
            model_discovery_checked_at=current.model_discovery_checked_at,
        )


class _StepRuns:
    def __init__(self, data: dict[UUID, StepRun]) -> None:
        self._data = data

    def add_many(self, steps: Sequence[StepRun]) -> None:
        for step in steps:
            self._data[step.id] = step

    def list(self, pipeline_run_id: UUID) -> Sequence[StepRun]:
        return sorted(
            (s for s in self._data.values() if s.pipeline_run_id == pipeline_run_id),
            key=lambda s: (s.created_at, s.step_name),
        )

    def update(self, step: StepRun, *, expected_status: StepStatus) -> None:
        current = self._data.get(step.id)
        if current is None:
            raise NotFound("step run", step.id)
        if current.status is not expected_status:
            raise Conflict(
                f"step run {step.id} is {current.status.value}, not {expected_status.value}"
            )
        self._data[step.id] = step


class _Models:
    def __init__(self, data: dict[UUID, Model]) -> None:
        self._data = data

    def add(self, model: Model) -> None:
        if self.get_by_name(model.project_id, model.name) is not None:
            raise AlreadyExists("model", model.name)
        self._data[model.id] = model

    def get(self, model_id: UUID) -> Model | None:
        return self._data.get(model_id)

    def get_by_name(self, project_id: UUID, name: str) -> Model | None:
        return next(
            (m for m in self._data.values() if m.project_id == project_id and m.name == name), None
        )

    def list(self, project_id: UUID) -> Sequence[Model]:
        return sorted(
            (m for m in self._data.values() if m.project_id == project_id),
            key=lambda m: (m.created_at, m.id),
        )

    def list_all(self) -> Sequence[Model]:
        return sorted(self._data.values(), key=lambda m: (m.created_at, m.id))

    def update_thresholds(self, model: Model) -> None:
        if model.id not in self._data:
            raise NotFound("model", model.id)
        self._data[model.id] = replace(self._data[model.id], thresholds=model.thresholds)

    def update_alias_drift(self, model: Model) -> None:
        if model.id not in self._data:
            raise NotFound("model", model.id)
        self._data[model.id] = replace(self._data[model.id], alias_drift=model.alias_drift)


class _ModelVersions:
    def __init__(self, data: dict[UUID, ModelVersion]) -> None:
        self._data = data

    def add(self, version: ModelVersion) -> None:
        for other in self._data.values():
            if other.model_id != version.model_id:
                continue
            if other.version == version.version:
                raise AlreadyExists("model version", version.version)
            if version.external_ref is not None and other.external_ref == version.external_ref:
                raise AlreadyExists("model version", version.external_ref)
        self._data[version.id] = version

    def get(self, version_id: UUID) -> ModelVersion | None:
        return self._data.get(version_id)

    def lock(self, version_id: UUID) -> ModelVersion | None:
        return self.get(version_id)

    def get_by_ref(self, model_id: UUID, external_ref: str) -> ModelVersion | None:
        return next(
            (
                v
                for v in self._data.values()
                if v.model_id == model_id and v.external_ref == external_ref
            ),
            None,
        )

    def list(self, model_id: UUID) -> Sequence[ModelVersion]:
        return sorted(
            (v for v in self._data.values() if v.model_id == model_id), key=lambda v: v.version
        )

    def next_version(self, model_id: UUID) -> int:
        return (
            max((v.version for v in self._data.values() if v.model_id == model_id), default=0) + 1
        )

    def get_champion(self, model_id: UUID) -> ModelVersion | None:
        return next(
            (
                v
                for v in self._data.values()
                if v.model_id == model_id and v.status is ModelStatus.CHAMPION
            ),
            None,
        )

    def update(self, version: ModelVersion, *, expected_status: ModelStatus) -> None:
        current = self._data.get(version.id)
        if current is None:
            raise NotFound("model version", version.id)
        if current.status is not expected_status:
            raise Conflict(
                f"model version {version.id} is {current.status.value}, not {expected_status.value}"
            )
        if version.status is ModelStatus.CHAMPION and any(
            v.model_id == version.model_id
            and v.status is ModelStatus.CHAMPION
            and v.id != version.id
            for v in self._data.values()
        ):
            raise Conflict(f"model {version.model_id} already has a champion")
        self._data[version.id] = version


class _Evaluations:
    def __init__(self, data: dict[UUID, Evaluation]) -> None:
        self._data = data

    def add(self, evaluation: Evaluation) -> None:
        self._data[evaluation.id] = evaluation

    def get(self, evaluation_id: UUID) -> Evaluation | None:
        return self._data.get(evaluation_id)

    def list_for_version(self, version_id: UUID) -> Sequence[Evaluation]:
        return sorted(
            (e for e in self._data.values() if e.model_version_id == version_id),
            key=lambda e: (e.created_at, e.id),
        )

    def update(self, evaluation: Evaluation, *, expected_status: EvaluationStatus) -> None:
        current = self._data.get(evaluation.id)
        if current is None:
            raise NotFound("evaluation", evaluation.id)
        if current.status is not expected_status:
            raise Conflict(
                f"evaluation {evaluation.id} is {current.status.value}, not {expected_status.value}"
            )
        self._data[evaluation.id] = evaluation


class _Promotions:
    def __init__(self, data: dict[UUID, Promotion]) -> None:
        self._data = data

    def add(self, promotion: Promotion) -> None:
        self._data[promotion.id] = promotion

    def list_for_versions(self, version_ids: Sequence[UUID]) -> Sequence[Promotion]:
        wanted = set(version_ids)
        return sorted(
            (p for p in self._data.values() if p.model_version_id in wanted),
            key=lambda p: (p.created_at, p.id),
        )


class _Deployments:
    def __init__(self, data: dict[UUID, Deployment]) -> None:
        self._data = data

    def add(self, deployment: Deployment) -> None:
        if self.get_by_name(deployment.project_id, deployment.name) is not None:
            raise AlreadyExists("deployment", deployment.name)
        self._data[deployment.id] = deployment

    def get(self, deployment_id: UUID) -> Deployment | None:
        return self._data.get(deployment_id)

    def lock(self, deployment_id: UUID) -> Deployment | None:
        return self.get(deployment_id)

    def get_by_name(self, project_id: UUID, name: str) -> Deployment | None:
        return next(
            (d for d in self._data.values() if d.project_id == project_id and d.name == name), None
        )

    def list(self, project_id: UUID) -> Sequence[Deployment]:
        return sorted(
            (d for d in self._data.values() if d.project_id == project_id),
            key=lambda d: (d.created_at, d.id),
        )

    def list_reconcilable(self) -> Sequence[Deployment]:
        return sorted(
            (
                d
                for d in self._data.values()
                if d.status is DeploymentStatus.DELETING
                or (d.desired_revision is not None and d.status is not DeploymentStatus.DELETED)
            ),
            key=lambda d: (d.created_at, d.id),
        )

    def update(self, deployment: Deployment, *, expected_status: DeploymentStatus) -> None:
        current = self._data.get(deployment.id)
        if current is None:
            raise NotFound("deployment", deployment.id)
        if current.status is not expected_status:
            raise Conflict(
                f"deployment {deployment.id} is {current.status.value}, not {expected_status.value}"
            )
        self._data[deployment.id] = deployment


class _Revisions:
    def __init__(self, data: dict[UUID, DeploymentRevision]) -> None:
        self._data = data

    def add(self, revision: DeploymentRevision) -> None:
        if self.get(revision.deployment_id, revision.revision) is not None:
            raise AlreadyExists("revision", revision.revision)
        self._data[revision.id] = revision

    def get(self, deployment_id: UUID, revision: int) -> DeploymentRevision | None:
        return next(
            (
                r
                for r in self._data.values()
                if r.deployment_id == deployment_id and r.revision == revision
            ),
            None,
        )

    def list(self, deployment_id: UUID) -> Sequence[DeploymentRevision]:
        return sorted(
            (r for r in self._data.values() if r.deployment_id == deployment_id),
            key=lambda r: r.revision,
        )

    def next_revision(self, deployment_id: UUID) -> int:
        return (
            max(
                (r.revision for r in self._data.values() if r.deployment_id == deployment_id),
                default=0,
            )
            + 1
        )


class _Endpoints:
    def __init__(self, data: dict[UUID, Endpoint]) -> None:
        self._data = data

    def add(self, endpoint: Endpoint) -> None:
        if self.get_by_name(endpoint.project_id, endpoint.name) is not None:
            raise AlreadyExists("endpoint", endpoint.name)
        self._data[endpoint.id] = endpoint

    def get_by_deployment(self, deployment_id: UUID) -> Endpoint | None:
        return next((e for e in self._data.values() if e.deployment_id == deployment_id), None)

    def get_by_name(self, project_id: UUID, name: str) -> Endpoint | None:
        return next(
            (e for e in self._data.values() if e.project_id == project_id and e.name == name), None
        )

    def update_lifecycle(self, endpoint: Endpoint, *, expected_status: EndpointStatus) -> None:
        current = self._data.get(endpoint.id)
        if current is None:
            raise NotFound("endpoint", endpoint.id)
        if current.status is not expected_status:
            raise Conflict(
                f"endpoint {endpoint.id} is {current.status.value}, not {expected_status.value}"
            )
        self._data[endpoint.id] = replace(
            endpoint, exposure=current.exposure, limits=current.limits
        )

    def initialize_limits(self, endpoint: Endpoint, *, expected_updated_at: datetime) -> None:
        current = self._data.get(endpoint.id)
        if (
            current is not None
            and current.limits == EndpointLimits()
            and current.updated_at == expected_updated_at
        ):
            self._data[endpoint.id] = replace(current, limits=endpoint.limits)

    def update_access(self, endpoint: Endpoint) -> None:
        current = self._data.get(endpoint.id)
        if current is None:
            raise NotFound("endpoint", endpoint.id)
        self._data[endpoint.id] = replace(
            current,
            exposure=endpoint.exposure,
            limits=endpoint.limits,
            updated_at=endpoint.updated_at,
        )


class _Rollouts:
    def __init__(self, data: dict[UUID, Rollout]) -> None:
        self._data = data

    def add(self, rollout: Rollout) -> None:
        if self.get_active(rollout.deployment_id) is not None or any(
            r.model_version_id == rollout.model_version_id and not r.is_terminal
            for r in self._data.values()
        ):
            raise AlreadyExists("rollout", rollout.deployment_id)
        self._data[rollout.id] = rollout

    def get(self, rollout_id: UUID) -> Rollout | None:
        return self._data.get(rollout_id)

    def get_active(self, deployment_id: UUID) -> Rollout | None:
        return next(
            (
                r
                for r in self._data.values()
                if r.deployment_id == deployment_id and not r.is_terminal
            ),
            None,
        )

    def get_active_by_version(self, version_id: UUID) -> Rollout | None:
        return next(
            (
                r
                for r in self._data.values()
                if r.model_version_id == version_id and not r.is_terminal
            ),
            None,
        )

    def list(self, deployment_id: UUID) -> Sequence[Rollout]:
        return sorted(
            (r for r in self._data.values() if r.deployment_id == deployment_id),
            key=lambda r: (r.created_at, r.id),
            reverse=True,
        )

    def list_active(self) -> Sequence[Rollout]:
        return sorted(
            (r for r in self._data.values() if not r.is_terminal),
            key=lambda r: (r.created_at, r.id),
        )

    def update(self, rollout: Rollout, *, expected_status: RolloutStatus) -> None:
        current = self._data.get(rollout.id)
        if current is None:
            raise NotFound("rollout", rollout.id)
        if current.status is not expected_status:
            raise Conflict(
                f"rollout {rollout.id} is {current.status.value}, not {expected_status.value}"
            )
        self._data[rollout.id] = rollout


class _Audit:
    def __init__(self, data: list[AuditEvent]) -> None:
        self._data = data

    def latest(
        self, *, project_id: UUID, entity_type: str, entity_id: UUID, actions: Sequence[str]
    ) -> AuditEvent | None:
        return max(
            (
                e
                for e in self._data
                if e.project_id == project_id
                and e.entity_type == entity_type
                and e.entity_id == entity_id
                and e.action in actions
            ),
            key=lambda e: (e.occurred_at, e.id),
            default=None,
        )

    def record(self, event: AuditEvent) -> None:
        self._data.append(event)

    def list(
        self, *, project_id: UUID | None = None, entity_id: UUID | None = None
    ) -> Sequence[AuditEvent]:
        return [
            e
            for e in self._data
            if (project_id is None or e.project_id == project_id)
            and (entity_id is None or e.entity_id == entity_id)
        ]


class _NotificationReads:
    def __init__(self, data: dict[tuple[str, str], datetime]) -> None:
        self._data = data

    def find(self, username: str, ids: Sequence[str]) -> set[str]:
        return {key for key in ids if (username, key) in self._data}

    def mark(self, username: str, ids: Sequence[str], at: datetime) -> None:
        for key in ids:
            self._data.setdefault((username, key), at)


class MemoryUnitOfWork:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def __enter__(self) -> Self:
        from controlplane.persistence.memory_data import MemoryDataCatalog
        from controlplane.persistence.memory_monitoring import MemoryMonitoring

        self._monitoring_reports = dict(self._store.monitoring_reports)
        self.monitoring = MemoryMonitoring(self._monitoring_reports)
        self._data_connections = dict(self._store.data_connections)
        self._dataset_versions = dict(self._store.dataset_versions)
        self.data_catalog = MemoryDataCatalog(self._data_connections, self._dataset_versions)
        self._notification_reads = dict(self._store.notification_reads)
        self.notification_reads = _NotificationReads(self._notification_reads)
        from controlplane.persistence.memory_schedules import MemorySchedules

        self._schedules = dict(self._store.schedules)
        self._schedule_executions = dict(self._store.schedule_executions)
        self.schedules = MemorySchedules(self._schedules, self._schedule_executions, self)
        self._projects = dict(self._store.projects)
        self._jobs = dict(self._store.jobs)
        self._runs = dict(self._store.runs)
        self._pipelines = dict(self._store.pipelines)
        self._pipeline_runs = dict(self._store.pipeline_runs)
        self._step_runs = dict(self._store.step_runs)
        self._models = dict(self._store.models)
        self._model_versions = dict(self._store.model_versions)
        self._evaluations = dict(self._store.evaluations)
        self._promotions = dict(self._store.promotions)
        self._deployments = dict(self._store.deployments)
        self._revisions = dict(self._store.revisions)
        self._endpoints = dict(self._store.endpoints)
        self._rollouts = dict(self._store.rollouts)
        self._audit = list(self._store.audit)
        self._memberships = dict(self._store.memberships)
        self.memberships = _Memberships(self._memberships)
        self._api_keys = dict(self._store.api_keys)
        self.api_keys = _ApiKeys(self._api_keys)
        self.projects = _Projects(self._projects)
        self.jobs = _Jobs(self._jobs)
        self.runs = _Runs(self._runs)
        self.pipelines = _Pipelines(self._pipelines)
        self.pipeline_runs = _PipelineRuns(self._pipeline_runs, self._pipelines)
        self.step_runs = _StepRuns(self._step_runs)
        self.models = _Models(self._models)
        self.model_versions = _ModelVersions(self._model_versions)
        self.evaluations = _Evaluations(self._evaluations)
        self.promotions = _Promotions(self._promotions)
        self.deployments = _Deployments(self._deployments)
        self.revisions = _Revisions(self._revisions)
        self.endpoints = _Endpoints(self._endpoints)
        self.rollouts = _Rollouts(self._rollouts)
        self.audit = _Audit(self._audit)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None  # uncommitted work is simply dropped

    def commit(self) -> None:
        self._store.monitoring_reports = self._monitoring_reports
        self._store.data_connections = self._data_connections
        self._store.dataset_versions = self._dataset_versions
        self._store.notification_reads = self._notification_reads
        self._store.schedules = self._schedules
        self._store.schedule_executions = self._schedule_executions
        self._store.projects = self._projects
        self._store.jobs = self._jobs
        self._store.runs = self._runs
        self._store.pipelines = self._pipelines
        self._store.pipeline_runs = self._pipeline_runs
        self._store.step_runs = self._step_runs
        self._store.models = self._models
        self._store.model_versions = self._model_versions
        self._store.evaluations = self._evaluations
        self._store.promotions = self._promotions
        self._store.deployments = self._deployments
        self._store.revisions = self._revisions
        self._store.endpoints = self._endpoints
        self._store.rollouts = self._rollouts
        self._store.audit = self._audit
        self._store.memberships = self._memberships
        self._store.api_keys = self._api_keys
