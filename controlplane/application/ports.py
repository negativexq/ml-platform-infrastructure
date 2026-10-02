"""Persistence ports. The application layer owns these; `persistence/` implements them."""

from __future__ import annotations

from collections.abc import Sequence
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import JobDefinition, Project, Run
from controlplane.domain.states import ProjectStatus, RunStatus


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
    def audit(self) -> AuditLog: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...
