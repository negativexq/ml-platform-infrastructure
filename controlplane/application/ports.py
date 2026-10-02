"""Persistence ports. The application layer owns these; `persistence/` implements them."""

from __future__ import annotations

from collections.abc import Sequence
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import Project


class ProjectRepository(Protocol):
    def add(self, project: Project) -> None:
        """Raises AlreadyExists if the name is taken."""

    def get(self, project_id: UUID) -> Project | None: ...

    def get_by_name(self, name: str) -> Project | None: ...

    def list(self, *, limit: int, offset: int) -> Sequence[Project]: ...


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
    def audit(self) -> AuditLog: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    def commit(self) -> None: ...
