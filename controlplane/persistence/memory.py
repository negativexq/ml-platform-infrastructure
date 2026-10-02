"""In-memory unit of work: the fake that lets every unit test run without a database.

Reads and writes go to a private copy that replaces the shared store only on
commit, so rollback semantics match the SQL implementation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self
from uuid import UUID

from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import Project
from controlplane.domain.errors import AlreadyExists


@dataclass
class MemoryStore:
    projects: dict[UUID, Project] = field(default_factory=dict)
    audit: list[AuditEvent] = field(default_factory=list)


class _Projects:
    def __init__(self, data: dict[UUID, Project]) -> None:
        self._data = data

    def add(self, project: Project) -> None:
        if any(p.name == project.name for p in self._data.values()):
            raise AlreadyExists("project", project.name)
        self._data[project.id] = project

    def get(self, project_id: UUID) -> Project | None:
        return self._data.get(project_id)

    def get_by_name(self, name: str) -> Project | None:
        return next((p for p in self._data.values() if p.name == name), None)

    def list(self, *, limit: int, offset: int) -> Sequence[Project]:
        ordered = sorted(self._data.values(), key=lambda p: (p.created_at, p.id))
        return ordered[offset : offset + limit]


class _Audit:
    def __init__(self, data: list[AuditEvent]) -> None:
        self._data = data

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


class MemoryUnitOfWork:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def __enter__(self) -> Self:
        self._projects = dict(self._store.projects)
        self._audit = list(self._store.audit)
        self.projects = _Projects(self._projects)
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
        self._store.projects = self._projects
        self._store.audit = self._audit
