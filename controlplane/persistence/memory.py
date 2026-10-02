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
from controlplane.domain.entities import JobDefinition, Project, Run
from controlplane.domain.errors import AlreadyExists, Conflict, NotFound
from controlplane.domain.states import ProjectStatus, RunStatus


@dataclass
class MemoryStore:
    projects: dict[UUID, Project] = field(default_factory=dict)
    jobs: dict[UUID, JobDefinition] = field(default_factory=dict)
    runs: dict[UUID, Run] = field(default_factory=dict)
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
        self, project_id: UUID, *, job_id: UUID | None, limit: int, offset: int
    ) -> Sequence[Run]:
        rows = [
            r
            for r in self._data.values()
            if r.project_id == project_id and (job_id is None or r.job_definition_id == job_id)
        ]
        rows.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return rows[offset : offset + limit]

    def list_active(self) -> Sequence[Run]:
        return sorted(
            (r for r in self._data.values() if not r.is_terminal),
            key=lambda r: (r.created_at, r.id),
        )

    def update(self, run: Run, *, expected_status: RunStatus) -> None:
        current = self._data.get(run.id)
        if current is None:
            raise NotFound("run", run.id)
        if current.status is not expected_status:
            raise Conflict(f"run {run.id} is {current.status.value}, not {expected_status.value}")
        self._data[run.id] = run


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
        self._jobs = dict(self._store.jobs)
        self._runs = dict(self._store.runs)
        self._audit = list(self._store.audit)
        self.projects = _Projects(self._projects)
        self.jobs = _Jobs(self._jobs)
        self.runs = _Runs(self._runs)
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
        self._store.jobs = self._jobs
        self._store.runs = self._runs
        self._store.audit = self._audit
