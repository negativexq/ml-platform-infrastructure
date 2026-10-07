"""In-memory scheduling repository for isolated tests and demo runs."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from controlplane.domain.errors import AlreadyExists
from controlplane.domain.schedules import (
    ConcurrencyScope,
    ExecutionStatus,
    Schedule,
    ScheduleExecution,
    TargetKind,
)

ACTIVE = ("PENDING", "SUBMITTED", "RUNNING")


class MemorySchedules:
    def __init__(
        self, schedules: dict[UUID, Schedule], executions: dict[UUID, ScheduleExecution], uow: Any
    ) -> None:
        self.schedules, self.executions, self.uow = schedules, executions, uow

    def get(self, id: UUID, *, lock: bool = False, skip_locked: bool = False) -> Schedule | None:
        return self.schedules.get(id)

    def add(self, entity: Schedule) -> None:
        if any(
            s.project_id == entity.project_id and s.name == entity.name
            for s in self.schedules.values()
        ):
            raise AlreadyExists("schedule", entity.name)
        self.schedules[entity.id] = entity

    def save(self, entity: Schedule) -> None:
        self.schedules[entity.id] = entity

    def list(
        self,
        project_ids: Sequence[UUID] | None,
        limit: int,
        offset: int,
        *,
        target_kind: TargetKind | None = None,
        target_name: str | None = None,
        paused: bool | None = None,
    ) -> Sequence[Schedule]:
        data = [
            s for s in self.schedules.values() if project_ids is None or s.project_id in project_ids
        ]
        data = [
            s
            for s in data
            if (target_kind is None or s.target_kind == target_kind)
            and (target_name is None or s.target_name == target_name)
            and (paused is None or s.paused == paused)
        ]
        return sorted(data, key=lambda s: (s.created_at, s.id), reverse=True)[
            offset : offset + limit
        ]

    def due(self, now: datetime, limit: int) -> Sequence[UUID]:
        return [
            s.id
            for s in sorted(self.schedules.values(), key=lambda s: (s.next_run_at, s.id))
            if not s.paused and s.next_run_at <= now
        ][:limit]

    def for_run(self, run_id: UUID, kind: TargetKind) -> ScheduleExecution | None:
        return next(
            (
                e
                for e in self.executions.values()
                if (e.pipeline_run_id if kind == TargetKind.PIPELINE else e.job_run_id) == run_id
            ),
            None,
        )

    def execution(self, id: UUID) -> ScheduleExecution | None:
        return self.executions.get(id)

    def occurrence(self, schedule_id: UUID, at: datetime) -> ScheduleExecution | None:
        return next(
            (
                e
                for e in self.executions.values()
                if e.schedule_id == schedule_id and e.scheduled_for_utc == at
            ),
            None,
        )

    def add_execution(self, entity: ScheduleExecution) -> None:
        if self.occurrence(entity.schedule_id, entity.scheduled_for_utc):
            raise AlreadyExists("schedule execution", entity.scheduled_for_utc)
        self.executions[entity.id] = entity

    def save_execution(self, entity: ScheduleExecution) -> None:
        self.executions[entity.id] = entity

    def history(self, schedule_id: UUID, limit: int, offset: int) -> Sequence[ScheduleExecution]:
        data = [e for e in self.executions.values() if e.schedule_id == schedule_id]
        return sorted(data, key=lambda e: (e.scheduled_for_utc, e.id), reverse=True)[
            offset : offset + limit
        ]

    def queued(self, limit: int) -> Sequence[ScheduleExecution]:
        candidates = sorted(
            [
                e
                for e in self.executions.values()
                if e.status == ExecutionStatus.QUEUED and not self.schedules[e.schedule_id].paused
            ],
            key=lambda e: (e.scheduled_for_utc, e.id),
        )
        heads: dict[UUID, ScheduleExecution] = {}
        for item in candidates:
            heads.setdefault(item.schedule_id, item)
        return list(heads.values())[:limit]

    def coordinate(self, entity: ScheduleExecution) -> bool:
        return True  # PostgreSQL tests exercise actual cross-dispatcher coordination.

    def _scope(self, e: ScheduleExecution, entity: ScheduleExecution) -> bool:
        return (
            e.schedule_id == entity.schedule_id
            if entity.concurrency_scope == ConcurrencyScope.SCHEDULE
            else (e.project_id, e.target_kind, e.target_name)
            == (entity.project_id, entity.target_kind, entity.target_name)
        )

    def active(self, entity: ScheduleExecution) -> bool:
        for e in self.executions.values():
            if e.status == ExecutionStatus.DISPATCHED and self._scope(e, entity):
                run = (
                    self.uow.pipeline_runs.get(e.pipeline_run_id)
                    if e.pipeline_run_id
                    else self.uow.runs.get(e.job_run_id)
                )
                if run and run.status.value in ACTIVE:
                    return True
        return False

    def queue_size(self, entity: ScheduleExecution) -> int:
        return sum(
            e.status == ExecutionStatus.QUEUED and self._scope(e, entity)
            for e in self.executions.values()
        )

    def older_queued(self, entity: ScheduleExecution) -> bool:
        return any(
            self._scope(e, entity)
            and (e.scheduled_for_utc, e.id) < (entity.scheduled_for_utc, entity.id)
            for e in self.queued(len(self.executions))
        )
