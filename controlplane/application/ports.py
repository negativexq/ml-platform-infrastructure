"""Persistence ports. The application layer owns these; `persistence/` implements them."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from controlplane.application.automation_ports import MonitoringAutomationRepository
from controlplane.application.data_ports import DataCatalogRepository
from controlplane.application.monitoring_ports import MonitoringRepository
from controlplane.application.schedule_ports import ScheduleRepository
from controlplane.domain.access import Membership
from controlplane.domain.api_keys import ApiKey
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import (
    Deployment,
    DeploymentRevision,
    Endpoint,
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


class ProjectRepository(Protocol):
    def add(self, project: Project) -> None:
        """Raises AlreadyExists if the name is taken."""

    def get(self, project_id: UUID) -> Project | None: ...

    def lock(self, project_id: UUID) -> Project | None: ...

    def get_by_name(self, name: str) -> Project | None: ...

    def list(self, *, limit: int, offset: int) -> Sequence[Project]:
        """Live projects only; DELETED projects are history, not inventory."""

    def list_reconcilable(self) -> Sequence[Project]:
        """Every project that is not DELETED."""

    def update(self, project: Project, *, expected_status: ProjectStatus) -> None:
        """Compare-and-swap on status. Conflict if another writer moved the project first."""


class JobRepository(Protocol):
    def add(self, job: JobDefinition) -> None:
        """Raises AlreadyExists if the project already has a job with this name."""

    def get(self, job_id: UUID) -> JobDefinition | None: ...

    def get_by_name(self, project_id: UUID, name: str) -> JobDefinition | None: ...

    def list(self, project_id: UUID) -> Sequence[JobDefinition]: ...


class RunRepository(Protocol):
    def add(self, run: Run) -> None:
        """Raises AlreadyExists if (project, idempotency_key) is already used."""

    def get(self, run_id: UUID) -> Run | None: ...

    def get_by_idempotency_key(self, project_id: UUID, key: str) -> Run | None: ...

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
        """Newest first."""

    def count(self, project_id: UUID) -> int: ...

    def list_cleanup_candidates(self, before: datetime, limit: int) -> Sequence[Run]: ...

    def list_active(self) -> Sequence[Run]:
        """Runs that are not in a terminal state, oldest first."""

    def update(self, run: Run, *, expected_status: RunStatus) -> None:
        """Compare-and-swap on status. Conflict if another writer moved the run first."""


class PipelineRepository(Protocol):
    def add(self, definition: PipelineDefinition) -> None:
        """Raises AlreadyExists if (project, name, version) exists."""

    def get(self, definition_id: UUID) -> PipelineDefinition | None: ...

    def get_version(
        self, project_id: UUID, name: str, version: int | None
    ) -> PipelineDefinition | None:
        """`version=None` is the latest version."""

    def list_latest(self, project_id: UUID) -> Sequence[PipelineDefinition]:
        """The newest version of every pipeline in the project."""


class PipelineRunRepository(Protocol):
    def add(self, run: PipelineRun) -> None:
        """Raises AlreadyExists if (project, idempotency_key) is already used."""

    def get(self, run_id: UUID) -> PipelineRun | None: ...

    def get_by_idempotency_key(self, project_id: UUID, key: str) -> PipelineRun | None: ...

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
        """Newest first."""

    def count(self, project_id: UUID) -> int: ...

    def list_cleanup_candidates(self, before: datetime, limit: int) -> Sequence[PipelineRun]: ...

    def list_discovery_candidates(self, before: datetime, limit: int) -> Sequence[PipelineRun]: ...

    def mark_model_discovery(self, run_id: UUID, checked_at: datetime, completed: bool) -> bool: ...

    def list_active(self) -> Sequence[PipelineRun]: ...

    def update(self, run: PipelineRun, *, expected_status: RunStatus) -> None:
        """Compare-and-swap on status."""


class StepRunRepository(Protocol):
    def add_many(self, steps: Sequence[StepRun]) -> None: ...

    def list(self, pipeline_run_id: UUID) -> Sequence[StepRun]: ...

    def update(self, step: StepRun, *, expected_status: StepStatus) -> None:
        """Compare-and-swap on status."""


class ModelRepository(Protocol):
    def add(self, model: Model) -> None:
        """Raises AlreadyExists if the project already has a model with this name."""

    def get(self, model_id: UUID) -> Model | None: ...

    def get_by_name(self, project_id: UUID, name: str) -> Model | None: ...

    def list(self, project_id: UUID) -> Sequence[Model]: ...

    def list_all(self) -> Sequence[Model]: ...

    def update_thresholds(self, model: Model) -> None:
        """Update thresholds only; never overwrite the reconciler-owned drift flag."""

    def update_alias_drift(self, model: Model) -> None: ...


class ModelVersionRepository(Protocol):
    def add(self, version: ModelVersion) -> None:
        """Raises AlreadyExists if (model, version) or (model, external_ref) exists."""

    def get(self, version_id: UUID) -> ModelVersion | None: ...

    def lock(self, version_id: UUID) -> ModelVersion | None: ...

    def get_by_ref(self, model_id: UUID, external_ref: str) -> ModelVersion | None: ...

    def list(self, model_id: UUID) -> Sequence[ModelVersion]:
        """Oldest first."""

    def next_version(self, model_id: UUID) -> int: ...

    def get_champion(self, model_id: UUID) -> ModelVersion | None: ...

    def update(self, version: ModelVersion, *, expected_status: ModelStatus) -> None:
        """Compare-and-swap on status."""


class EvaluationRepository(Protocol):
    def add(self, evaluation: Evaluation) -> None: ...

    def get(self, evaluation_id: UUID) -> Evaluation | None: ...

    def list_for_version(self, version_id: UUID) -> Sequence[Evaluation]:
        """Oldest first."""

    def update(self, evaluation: Evaluation, *, expected_status: EvaluationStatus) -> None:
        """Compare-and-swap on status."""


class PromotionRepository(Protocol):
    def add(self, promotion: Promotion) -> None: ...

    def list_for_versions(self, version_ids: Sequence[UUID]) -> Sequence[Promotion]:
        """Oldest first."""


class DeploymentRepository(Protocol):
    def add(self, deployment: Deployment) -> None:
        """Raises AlreadyExists if the project already has a deployment with this name."""

    def get(self, deployment_id: UUID) -> Deployment | None: ...

    def lock(self, deployment_id: UUID) -> Deployment | None: ...

    def get_by_name(self, project_id: UUID, name: str) -> Deployment | None: ...

    def list(self, project_id: UUID) -> Sequence[Deployment]: ...

    def list_reconcilable(self) -> Sequence[Deployment]:
        """Deployments that have a desired revision to make real."""

    def update(self, deployment: Deployment, *, expected_status: DeploymentStatus) -> None:
        """Compare-and-swap on status."""


class RevisionRepository(Protocol):
    """Append-only: there is deliberately no update."""

    def add(self, revision: DeploymentRevision) -> None:
        """Raises AlreadyExists if (deployment, revision) exists."""

    def get(self, deployment_id: UUID, revision: int) -> DeploymentRevision | None: ...

    def list(self, deployment_id: UUID) -> Sequence[DeploymentRevision]:
        """Oldest first."""

    def next_revision(self, deployment_id: UUID) -> int: ...


class EndpointRepository(Protocol):
    def add(self, endpoint: Endpoint) -> None: ...

    def get_by_deployment(self, deployment_id: UUID) -> Endpoint | None: ...

    def get_by_name(self, project_id: UUID, name: str) -> Endpoint | None: ...

    def update_lifecycle(self, endpoint: Endpoint, *, expected_status: EndpointStatus) -> None:
        """Lifecycle fields only, compare-and-swap on status."""

    def initialize_limits(self, endpoint: Endpoint, *, expected_updated_at: datetime) -> None:
        """Set runtime defaults only while limits still equal the initial defaults."""

    def update_access(self, endpoint: Endpoint) -> None:
        """Exposure/limits only; preserves lifecycle fields."""


class RolloutRepository(Protocol):
    def add(self, rollout: Rollout) -> None:
        """Raises AlreadyExists if the deployment already has a rollout in flight."""

    def get(self, rollout_id: UUID) -> Rollout | None: ...

    def get_active(self, deployment_id: UUID) -> Rollout | None:
        """The PENDING or PROGRESSING rollout of a deployment, if any."""

    def get_active_by_version(self, version_id: UUID) -> Rollout | None:
        """The active reservation for a model version, if any."""

    def list(self, deployment_id: UUID) -> Sequence[Rollout]:
        """Newest first."""

    def list_active(self) -> Sequence[Rollout]: ...

    def update(self, rollout: Rollout, *, expected_status: RolloutStatus) -> None:
        """Compare-and-swap on status."""


class AuditLog(Protocol):
    def latest(
        self, *, project_id: UUID, entity_type: str, entity_id: UUID, actions: Sequence[str]
    ) -> AuditEvent | None: ...

    def record(self, event: AuditEvent) -> None: ...

    def list(
        self, *, project_id: UUID | None = None, entity_id: UUID | None = None
    ) -> Sequence[AuditEvent]: ...


class MembershipRepository(Protocol):
    def add(self, membership: Membership) -> None:
        """Raises AlreadyExists if the subject is already a member of the project."""

    def get(self, project_id: UUID, subject: str) -> Membership | None: ...

    def list(self, project_id: UUID) -> Sequence[Membership]:
        """Ordered by subject."""

    def list_for_subjects(self, subjects: Collection[str]) -> Sequence[Membership]:
        """Every membership, in any project, held by any of these subjects."""

    def update(self, membership: Membership) -> None: ...

    def remove(self, project_id: UUID, subject: str) -> None: ...


class ApiKeyRepository(Protocol):
    def add(self, key: ApiKey) -> None:
        """AlreadyExists if the project has a key with that name."""

    def get(self, key_id: str) -> ApiKey | None: ...

    def list(self, project_id: UUID) -> Sequence[ApiKey]:
        """Newest first, revoked ones included."""

    def update(self, key: ApiKey) -> None: ...

    def touch(self, key_id: str, at: datetime) -> None: ...


class NotificationReadRepository(Protocol):
    def find(self, username: str, ids: Sequence[str]) -> set[str]: ...

    def mark(self, username: str, ids: Sequence[str], at: datetime) -> None: ...


class UnitOfWork(Protocol):
    """One transaction. Leaving the block without `commit()` rolls everything back."""

    @property
    def monitoring_automation(self) -> MonitoringAutomationRepository: ...

    @property
    def monitoring(self) -> MonitoringRepository: ...

    @property
    def data_catalog(self) -> DataCatalogRepository: ...

    @property
    def schedules(self) -> ScheduleRepository: ...

    @property
    def projects(self) -> ProjectRepository: ...

    @property
    def notification_reads(self) -> NotificationReadRepository: ...

    @property
    def memberships(self) -> MembershipRepository: ...

    @property
    def api_keys(self) -> ApiKeyRepository: ...

    @property
    def jobs(self) -> JobRepository: ...

    @property
    def runs(self) -> RunRepository: ...

    @property
    def pipelines(self) -> PipelineRepository: ...

    @property
    def pipeline_runs(self) -> PipelineRunRepository: ...

    @property
    def step_runs(self) -> StepRunRepository: ...

    @property
    def models(self) -> ModelRepository: ...

    @property
    def model_versions(self) -> ModelVersionRepository: ...

    @property
    def evaluations(self) -> EvaluationRepository: ...

    @property
    def promotions(self) -> PromotionRepository: ...

    @property
    def deployments(self) -> DeploymentRepository: ...

    @property
    def revisions(self) -> RevisionRepository: ...

    @property
    def endpoints(self) -> EndpointRepository: ...

    @property
    def rollouts(self) -> RolloutRepository: ...

    @property
    def audit(self) -> AuditLog: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...
