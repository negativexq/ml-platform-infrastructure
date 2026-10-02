"""Persistence ports. The application layer owns these; `persistence/` implements them."""

from __future__ import annotations

from collections.abc import Sequence
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import (
    Evaluation,
    JobDefinition,
    Model,
    ModelVersion,
    PipelineDefinition,
    PipelineRun,
    Project,
    Promotion,
    Run,
    StepRun,
)
from controlplane.domain.states import (
    EvaluationStatus,
    ModelStatus,
    ProjectStatus,
    RunStatus,
    StepStatus,
)


class ProjectRepository(Protocol):
    def add(self, project: Project) -> None:
        """Raises AlreadyExists if the name is taken."""

    def get(self, project_id: UUID) -> Project | None: ...

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
        self, project_id: UUID, *, job_id: UUID | None, limit: int, offset: int
    ) -> Sequence[Run]:
        """Newest first."""

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
        self, project_id: UUID, *, definition_ids: Sequence[UUID] | None, limit: int, offset: int
    ) -> Sequence[PipelineRun]:
        """Newest first."""

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

    def update(self, model: Model) -> None:
        """Replace thresholds / drift flag (the only mutable parts of a model)."""


class ModelVersionRepository(Protocol):
    def add(self, version: ModelVersion) -> None:
        """Raises AlreadyExists if (model, version) or (model, external_ref) exists."""

    def get(self, version_id: UUID) -> ModelVersion | None: ...

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


class AuditLog(Protocol):
    def record(self, event: AuditEvent) -> None: ...

    def list(
        self, *, project_id: UUID | None = None, entity_id: UUID | None = None
    ) -> Sequence[AuditEvent]: ...


class UnitOfWork(Protocol):
    """One transaction. Leaving the block without `commit()` rolls everything back."""

    @property
    def projects(self) -> ProjectRepository: ...

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
    def audit(self) -> AuditLog: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...
