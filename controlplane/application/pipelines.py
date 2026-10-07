from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from controlplane.application.identity import current_actor
from controlplane.application.jobs import resolve_project
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import PipelineDefinition, StepSpec
from controlplane.domain.errors import AlreadyExists, InvalidArgument, NotFound


@dataclass(frozen=True, slots=True)
class StepInput:
    name: str
    job: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CreatePipeline:
    name: str
    steps: Sequence[StepInput] = field(default_factory=tuple)
    parameter_schema: Mapping[str, Any] = field(default_factory=dict)


class PipelineService:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock = utc_now) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def create(self, project_ref: str, cmd: CreatePipeline) -> tuple[PipelineDefinition, bool]:
        """Create a new immutable version. Identical to the latest version ->
        that version, `created=False`; anything else -> version + 1."""
        for _ in range(3):  # a concurrent writer can take our version number
            try:
                return self._create(project_ref, cmd)
            except AlreadyExists:
                continue
        raise AlreadyExists("pipeline", cmd.name)

    def _create(self, project_ref: str, cmd: CreatePipeline) -> tuple[PipelineDefinition, bool]:
        steps = tuple(StepSpec(s.name, s.job, tuple(s.depends_on)) for s in cmd.steps)
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            latest = uow.pipelines.get_version(project.id, cmd.name, None)
            definition = PipelineDefinition.create(
                project_id=project.id,
                name=cmd.name,
                version=1 if latest is None else latest.version + 1,
                steps=steps,
                parameter_schema=cmd.parameter_schema,
                now=self._clock(),
            )
            if latest is not None and latest.same_content(definition):
                return latest, False
            unknown = sorted(
                {s.job for s in steps if uow.jobs.get_by_name(project.id, s.job) is None}
            )
            if unknown:
                raise InvalidArgument(f"unknown job definitions: {unknown}")
            uow.pipelines.add(definition)
            uow.audit.record(
                AuditEvent(
                    occurred_at=definition.created_at,
                    actor=current_actor(),
                    action="pipeline.created",
                    entity_type="pipeline",
                    entity_id=definition.id,
                    project_id=project.id,
                    payload={"name": definition.name, "version": definition.version},
                )
            )
            uow.commit()
            return definition, True

    def get(self, project_ref: str, name: str, version: int | None = None) -> PipelineDefinition:
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            definition = uow.pipelines.get_version(project.id, name, version)
        if definition is None:
            raise NotFound("pipeline", name if version is None else f"{name}@{version}")
        return definition

    def list(self, project_ref: str) -> Sequence[PipelineDefinition]:
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            return uow.pipelines.list_latest(project.id)
