"""Schedule CRUD and bounded transactional dispatcher; Argo remains the executor."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from controlplane.application.identity import bind_principal, current_actor, reset_principal
from controlplane.application.jobs import resolve_project
from controlplane.application.pipeline_runs import PipelineRunService
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.application.runs import RunService
from controlplane.application.schedule_calendar import next_occurrence
from controlplane.domain.access import Principal
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import (
    JobDefinition,
    PipelineDefinition,
    validate_slug,
    validate_timeout,
)
from controlplane.domain.errors import Conflict, InvalidArgument, NotFound
from controlplane.domain.parameters import fingerprint, project_values, resolve, scheduled_values
from controlplane.domain.schedules import (
    ConcurrencyPolicy,
    ExecutionStatus,
    MissedRunPolicy,
    Schedule,
    ScheduleExecution,
    TargetKind,
    VersionPolicy,
)
from controlplane.domain.states import ProjectStatus


def audit(
    uow: UnitOfWork,
    entity: Schedule | ScheduleExecution,
    action: str,
    now: datetime,
    *,
    actor: str | None = None,
    **payload: Any,
) -> None:
    uow.audit.record(
        AuditEvent(
            occurred_at=now,
            actor=actor or current_actor(),
            action=action,
            entity_type="schedule_execution"
            if isinstance(entity, ScheduleExecution)
            else "schedule",
            entity_id=entity.id,
            project_id=entity.project_id,
            payload=payload,
        )
    )


def resolve_definition(
    uow: UnitOfWork, schedule: Schedule
) -> JobDefinition | PipelineDefinition | None:
    if schedule.target_kind == TargetKind.JOB:
        return uow.jobs.get_by_name(schedule.project_id, schedule.target_name)
    return uow.pipelines.get_version(
        schedule.project_id,
        schedule.target_name,
        schedule.version if schedule.version_policy == VersionPolicy.PINNED else None,
    )


def occurrence_parameters(
    uow: UnitOfWork,
    schedule: Schedule,
    definition: JobDefinition | PipelineDefinition,
    at: datetime,
) -> dict[str, Any]:
    values = resolve(
        definition.parameter_schema,
        scheduled_values(schedule.parameters, schedule.parameter_bindings, at, schedule.timezone),
    )
    if isinstance(definition, PipelineDefinition):
        for step in definition.steps:
            job = uow.jobs.get_by_name(schedule.project_id, step.job)
            if job is None:
                raise InvalidArgument("pipeline job unavailable")
            project_values(job.parameter_schema, values)
    return values


def audit_configuration(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (fingerprint(value) if key == "parameters" else value) for key, value in values.items()
    }


class ScheduleService:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock = utc_now) -> None:
        self.uow_factory, self.clock = uow_factory, clock

    def create(self, project_ref: str, **spec: Any) -> Schedule:
        now = self.clock()
        with self.uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            if project.status != ProjectStatus.READY:
                raise Conflict("schedules need a READY project")
            entity = Schedule(
                project_id=project.id,
                created_at=now,
                updated_at=now,
                next_run_at=next_occurrence(spec["cron"], spec["timezone"], now),
                **spec,
            )
            self.validate(uow, entity)
            uow.schedules.add(entity)
            audit(
                uow,
                entity,
                "schedule.created",
                now,
                revision=entity.revision,
                configuration=audit_configuration(spec),
            )
            uow.commit()
            return entity

    @staticmethod
    def validate(uow: UnitOfWork, entity: Schedule) -> None:
        validate_slug(entity.name, "schedule name")
        validate_slug(entity.target_name, "target name")
        validate_timeout(entity.timeout_seconds)
        if not 1 <= entity.deadline_seconds <= 604800:
            raise InvalidArgument("deadline_seconds must be 1-604800")
        if not 1 <= entity.queue_ttl_seconds <= 604800 or not 1 <= entity.max_queue_size <= 1000:
            raise InvalidArgument("queue TTL must be 1-604800 seconds; queue size must be 1-1000")
        if (
            entity.target_kind == TargetKind.PIPELINE
            and entity.version_policy == VersionPolicy.PINNED
            and entity.version is None
        ):
            raise InvalidArgument("PINNED pipelines require a version")
        if entity.version_policy == VersionPolicy.LATEST and entity.version is not None:
            raise InvalidArgument("LATEST must not specify a version")
        if entity.target_kind == TargetKind.JOB and (
            entity.version is not None or entity.version_policy != VersionPolicy.PINNED
        ):
            raise InvalidArgument("jobs are immutable; use PINNED without a version")
        definition = resolve_definition(uow, entity)
        if definition is None:
            raise NotFound(entity.target_kind.value.lower(), entity.target_name)
        occurrence_parameters(uow, entity, definition, entity.next_run_at)

    def get(self, id: UUID) -> Schedule:
        with self.uow_factory() as uow:
            entity = uow.schedules.get(id)
            if entity is None:
                raise NotFound("schedule", id)
            return entity

    def list(
        self, project_ids: Sequence[UUID] | None, limit: int = 50, offset: int = 0, **filters: Any
    ) -> Sequence[Schedule]:
        with self.uow_factory() as uow:
            return uow.schedules.list(project_ids, limit, offset, **filters)

    def update(self, id: UUID, expected_revision: int, changes: dict[str, Any]) -> Schedule:
        now = self.clock()
        with self.uow_factory() as uow:
            entity = uow.schedules.get(id, lock=True)
            if entity is None:
                raise NotFound("schedule", id)
            if entity.revision != expected_revision:
                raise Conflict("schedule changed; reload before editing")
            allowed = {
                "cron",
                "timezone",
                "version_policy",
                "version",
                "concurrency_policy",
                "concurrency_scope",
                "missed_run_policy",
                "deadline_seconds",
                "queue_ttl_seconds",
                "max_queue_size",
                "timeout_seconds",
                "paused",
                "parameters",
                "parameter_bindings",
            }
            if set(changes) - allowed:
                raise InvalidArgument("schedule identity and target cannot be changed")
            updated = replace(entity, **changes, revision=entity.revision + 1, updated_at=now)
            if (updated.cron, updated.timezone) != (entity.cron, entity.timezone):
                updated = replace(
                    updated, next_run_at=next_occurrence(updated.cron, updated.timezone, now)
                )
            self.validate(uow, updated)
            uow.schedules.save(updated)
            audit(
                uow,
                updated,
                "schedule.updated",
                now,
                revision=updated.revision,
                changes=audit_configuration(changes),
            )
            uow.commit()
            return updated

    def history(self, id: UUID, limit: int = 50, offset: int = 0) -> Sequence[ScheduleExecution]:
        self.get(id)
        with self.uow_factory() as uow:
            return uow.schedules.history(id, limit, offset)


class ScheduleDispatcher:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock = utc_now) -> None:
        self.uow_factory, self.clock = uow_factory, clock

    def tick(self, **limits: int) -> dict[str, int]:
        token = bind_principal(Principal(username="schedule-dispatcher"))
        try:
            return self._tick(**limits)
        finally:
            reset_principal(token)

    def _tick(
        self, *, schedule_limit: int = 50, occurrence_limit: int = 20, queue_limit: int = 100
    ) -> dict[str, int]:
        now = self.clock()
        counts: Counter[str] = Counter()
        # Drain queues before creating newer occurrences, preserving FIFO across schedules.
        with self.uow_factory() as uow:
            queued = uow.schedules.queued(queue_limit)
            due = uow.schedules.due(now, schedule_limit)
        for candidate in queued:
            with self.uow_factory() as uow:
                schedule = uow.schedules.get(candidate.schedule_id, lock=True, skip_locked=True)
                if schedule is None or schedule.paused:
                    continue
                entity = uow.schedules.execution(candidate.id)
                if entity is None or entity.status != ExecutionStatus.QUEUED:
                    continue
                if not uow.schedules.coordinate(entity):
                    continue
                updated = self._dispatch(uow, entity, now)
                if updated != entity:
                    uow.schedules.save_execution(updated)
                    counts[updated.status.value] += 1
                uow.commit()
        for id in due:
            with self.uow_factory() as uow:
                schedule = uow.schedules.get(id, lock=True, skip_locked=True)
                if schedule is None or schedule.paused:
                    continue
                for _ in range(occurrence_limit):
                    at = schedule.next_run_at
                    if at > now:
                        break
                    following = next_occurrence(schedule.cron, schedule.timezone, at)
                    if uow.schedules.occurrence(schedule.id, at):
                        schedule = replace(schedule, next_run_at=following, updated_at=now)
                        continue
                    definition = resolve_definition(uow, schedule)
                    parameter_error = False
                    parameters = {}
                    if definition is not None:
                        try:
                            parameters = occurrence_parameters(uow, schedule, definition, at)
                        except InvalidArgument:
                            parameter_error = True
                    entity = ScheduleExecution(
                        parameters=parameters,
                        schedule_id=schedule.id,
                        project_id=schedule.project_id,
                        scheduled_for_utc=at,
                        schedule_revision=schedule.revision,
                        resolved_definition_id=definition.id if definition else None,
                        target_kind=schedule.target_kind,
                        target_name=schedule.target_name,
                        concurrency_policy=schedule.concurrency_policy,
                        concurrency_scope=schedule.concurrency_scope,
                        timeout_seconds=schedule.timeout_seconds,
                        expires_at=at + timedelta(seconds=schedule.queue_ttl_seconds),
                        created_at=now,
                        updated_at=now,
                    )
                    if not uow.schedules.coordinate(entity):
                        break  # never advance an occurrence we could not coordinate
                    if (now - at).total_seconds() > schedule.deadline_seconds:
                        entity = replace(
                            entity, status=ExecutionStatus.MISSED, reason="start deadline exceeded"
                        )
                    elif schedule.missed_run_policy == MissedRunPolicy.SKIP and following <= now:
                        entity = replace(
                            entity,
                            status=ExecutionStatus.MISSED,
                            reason="superseded by a newer occurrence",
                        )
                    elif parameter_error:
                        entity = replace(
                            entity,
                            status=ExecutionStatus.MISSED,
                            reason="parameter validation failed for resolved definition",
                        )
                    elif definition is None:
                        entity = replace(
                            entity,
                            status=ExecutionStatus.MISSED,
                            reason="target definition unavailable",
                        )
                    elif (
                        entity.concurrency_policy == ConcurrencyPolicy.QUEUE
                        and uow.schedules.queue_size(entity) >= schedule.max_queue_size
                    ):
                        entity = replace(
                            entity, status=ExecutionStatus.SKIPPED, reason="queue capacity exceeded"
                        )
                    uow.schedules.add_execution(entity)
                    if entity.status == ExecutionStatus.QUEUED:
                        entity = self._dispatch(uow, entity, now)
                        uow.schedules.save_execution(entity)
                    audit(
                        uow,
                        entity,
                        "schedule_execution.created",
                        now,
                        actor="schedule-dispatcher",
                        schedule_id=str(schedule.id),
                        scheduled_for=at.isoformat(),
                        status=entity.status.value,
                        definition_id=str(entity.resolved_definition_id),
                        revision=entity.schedule_revision,
                        reason=entity.reason,
                    )
                    counts[entity.status.value] += 1
                    schedule = replace(schedule, next_run_at=following, updated_at=now)
                uow.schedules.save(schedule)
                uow.commit()
        return dict(counts)

    def _dispatch(
        self, uow: UnitOfWork, entity: ScheduleExecution, now: datetime
    ) -> ScheduleExecution:
        try:
            return self._dispatch_checked(uow, entity, now)
        except (InvalidArgument, NotFound):
            return self._finish(
                uow,
                entity,
                ExecutionStatus.MISSED,
                "execution input unavailable or incompatible",
                now,
            )

    def _dispatch_checked(
        self, uow: UnitOfWork, entity: ScheduleExecution, now: datetime
    ) -> ScheduleExecution:
        if entity.concurrency_policy == ConcurrencyPolicy.QUEUE and entity.expires_at <= now:
            return self._finish(uow, entity, ExecutionStatus.MISSED, "queue deadline exceeded", now)
        project = uow.projects.get(entity.project_id)
        if project is None or project.status != ProjectStatus.READY:
            return self._finish(uow, entity, ExecutionStatus.MISSED, "project is not READY", now)
        busy = uow.schedules.active(entity)
        if entity.concurrency_policy == ConcurrencyPolicy.FORBID and busy:
            return self._finish(
                uow, entity, ExecutionStatus.SKIPPED, "concurrency limit reached", now
            )
        if entity.concurrency_policy == ConcurrencyPolicy.QUEUE and (
            busy or uow.schedules.older_queued(entity)
        ):
            return entity
        if entity.resolved_definition_id is None:
            return self._finish(
                uow, entity, ExecutionStatus.MISSED, "frozen definition unavailable", now
            )
        key = f"schedule:{entity.schedule_id}:{entity.scheduled_for_utc.isoformat()}"
        if entity.target_kind == TargetKind.PIPELINE:
            definition = uow.pipelines.get(entity.resolved_definition_id)
            if definition is None:
                return self._finish(
                    uow, entity, ExecutionStatus.MISSED, "frozen definition unavailable", now
                )
            view, _ = PipelineRunService(self.uow_factory, lambda: now).create_in_uow(
                uow,
                project.name,
                definition.name,
                definition.version,
                None,
                key,
                entity.timeout_seconds,
                entity.parameters,
            )
            updated = replace(
                entity,
                status=ExecutionStatus.DISPATCHED,
                pipeline_run_id=view.run.id,
                reason=None,
                updated_at=now,
            )
        else:
            job = uow.jobs.get(entity.resolved_definition_id)
            if job is None:
                return self._finish(
                    uow, entity, ExecutionStatus.MISSED, "frozen definition unavailable", now
                )
            run, _ = RunService(self.uow_factory, lambda: now).create_in_uow(
                uow, project.name, job.name, key, None, entity.timeout_seconds, entity.parameters
            )
            updated = replace(
                entity,
                status=ExecutionStatus.DISPATCHED,
                job_run_id=run.id,
                reason=None,
                updated_at=now,
            )
        audit(
            uow,
            updated,
            "schedule_execution.dispatched",
            now,
            actor="schedule-dispatcher",
            schedule_id=str(entity.schedule_id),
            pipeline_run_id=str(updated.pipeline_run_id),
            job_run_id=str(updated.job_run_id),
            scheduled_for=entity.scheduled_for_utc.isoformat(),
        )
        return updated

    @staticmethod
    def _finish(
        uow: UnitOfWork,
        entity: ScheduleExecution,
        status: ExecutionStatus,
        reason: str,
        now: datetime,
    ) -> ScheduleExecution:
        updated = replace(entity, status=status, reason=reason, updated_at=now)
        audit(
            uow,
            updated,
            "schedule_execution." + status.value.lower(),
            now,
            actor="schedule-dispatcher",
            reason=reason,
            schedule_id=str(entity.schedule_id),
        )
        return updated
