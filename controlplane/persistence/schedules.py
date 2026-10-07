"""Schedule persistence. All writes and target locks use the caller's transaction."""

from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from controlplane.domain.errors import AlreadyExists, NotFound
from controlplane.domain.schedules import (
    ConcurrencyPolicy,
    ConcurrencyScope,
    ExecutionStatus,
    MissedRunPolicy,
    Schedule,
    ScheduleExecution,
    TargetKind,
    VersionPolicy,
)
from controlplane.persistence.models import (
    PipelineRunRow,
    RunRow,
    ScheduleExecutionRow,
    ScheduleRow,
)

ACTIVE = ("PENDING", "SUBMITTED", "RUNNING")


def schedule(row: ScheduleRow) -> Schedule:
    spec = dict(row.spec)
    for key, enum in [
        ("target_kind", TargetKind),
        ("version_policy", VersionPolicy),
        ("concurrency_policy", ConcurrencyPolicy),
        ("concurrency_scope", ConcurrencyScope),
        ("missed_run_policy", MissedRunPolicy),
    ]:
        spec[key] = enum(spec[key])
    return Schedule(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        paused=row.paused,
        revision=row.revision,
        next_run_at=row.next_run_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        **spec,
    )


def execution(row: ScheduleExecutionRow) -> ScheduleExecution:
    spec = dict(row.spec)
    for key, enum in [
        ("target_kind", TargetKind),
        ("concurrency_policy", ConcurrencyPolicy),
        ("concurrency_scope", ConcurrencyScope),
    ]:
        spec[key] = enum(spec[key])
    return ScheduleExecution(
        id=row.id,
        schedule_id=row.schedule_id,
        project_id=row.project_id,
        scheduled_for_utc=row.scheduled_for_utc,
        status=ExecutionStatus(row.status),
        resolved_definition_id=row.resolved_definition_id,
        pipeline_run_id=row.pipeline_run_id,
        job_run_id=row.job_run_id,
        reason=row.reason,
        expires_at=row.expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        **spec,
    )


def values(
    entity: Schedule | ScheduleExecution, row_type: type[ScheduleRow] | type[ScheduleExecutionRow]
) -> dict[str, Any]:
    data = asdict(entity)
    columns = set(row_type.__table__.columns.keys()) - {"spec"}
    return {
        **{k: v for k, v in data.items() if k in columns},
        "spec": {k: v for k, v in data.items() if k not in columns},
    }


class SqlSchedules:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, id: UUID, *, lock: bool = False, skip_locked: bool = False) -> Schedule | None:
        query = select(ScheduleRow).where(ScheduleRow.id == id)
        if lock:
            query = query.with_for_update(skip_locked=skip_locked)
        row = self.session.scalar(query)
        return schedule(row) if row else None

    def add(self, entity: Schedule) -> None:
        if self.session.scalar(
            select(ScheduleRow.id).where(
                ScheduleRow.project_id == entity.project_id, ScheduleRow.name == entity.name
            )
        ):
            raise AlreadyExists("schedule", entity.name)
        self.session.add(ScheduleRow(**values(entity, ScheduleRow)))
        try:
            self.session.flush()
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == "23505":
                raise AlreadyExists("schedule", entity.name) from exc
            raise

    def save(self, entity: Schedule) -> None:
        self._save(entity, ScheduleRow)

    def _save(
        self,
        entity: Schedule | ScheduleExecution,
        row_type: type[ScheduleRow] | type[ScheduleExecutionRow],
    ) -> None:
        row = self.session.get(row_type, entity.id)
        if row is None:
            raise NotFound("schedule resource", entity.id)
        for key, value in values(entity, row_type).items():
            setattr(row, key, value)
        self.session.flush()

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
        query = select(ScheduleRow).order_by(ScheduleRow.created_at.desc(), ScheduleRow.id)
        if project_ids is not None:
            query = query.where(ScheduleRow.project_id.in_(project_ids))
        if target_kind is not None:
            query = query.where(ScheduleRow.spec["target_kind"].astext == target_kind)
        if target_name is not None:
            query = query.where(ScheduleRow.spec["target_name"].astext == target_name)
        if paused is not None:
            query = query.where(ScheduleRow.paused == paused)
        return [schedule(r) for r in self.session.scalars(query.limit(limit).offset(offset))]

    def due(self, now: datetime, limit: int) -> Sequence[UUID]:
        return list(
            self.session.scalars(
                select(ScheduleRow.id)
                .where(~ScheduleRow.paused, ScheduleRow.next_run_at <= now)
                .order_by(ScheduleRow.next_run_at, ScheduleRow.id)
                .limit(limit)
            )
        )

    def for_run(self, run_id: UUID, kind: TargetKind) -> ScheduleExecution | None:
        column = (
            ScheduleExecutionRow.pipeline_run_id
            if kind == TargetKind.PIPELINE
            else ScheduleExecutionRow.job_run_id
        )
        row = self.session.scalar(select(ScheduleExecutionRow).where(column == run_id))
        return execution(row) if row else None

    def execution(self, id: UUID) -> ScheduleExecution | None:
        row = self.session.get(ScheduleExecutionRow, id)
        return execution(row) if row else None

    def occurrence(self, schedule_id: UUID, at: datetime) -> ScheduleExecution | None:
        row = self.session.scalar(
            select(ScheduleExecutionRow).where(
                ScheduleExecutionRow.schedule_id == schedule_id,
                ScheduleExecutionRow.scheduled_for_utc == at,
            )
        )
        return execution(row) if row else None

    def add_execution(self, entity: ScheduleExecution) -> None:
        self.session.add(ScheduleExecutionRow(**values(entity, ScheduleExecutionRow)))
        self.session.flush()

    def save_execution(self, entity: ScheduleExecution) -> None:
        self._save(entity, ScheduleExecutionRow)

    def history(self, schedule_id: UUID, limit: int, offset: int) -> Sequence[ScheduleExecution]:
        return [
            execution(r)
            for r in self.session.scalars(
                select(ScheduleExecutionRow)
                .where(ScheduleExecutionRow.schedule_id == schedule_id)
                .order_by(ScheduleExecutionRow.scheduled_for_utc.desc(), ScheduleExecutionRow.id)
                .limit(limit)
                .offset(offset)
            )
        ]

    def queued(self, limit: int) -> Sequence[ScheduleExecution]:
        heads = (
            select(
                ScheduleExecutionRow.id,
                func.row_number()
                .over(
                    partition_by=ScheduleExecutionRow.schedule_id,
                    order_by=(ScheduleExecutionRow.scheduled_for_utc, ScheduleExecutionRow.id),
                )
                .label("position"),
            )
            .join(ScheduleRow, ScheduleRow.id == ScheduleExecutionRow.schedule_id)
            .where(~ScheduleRow.paused, ScheduleExecutionRow.status == "QUEUED")
            .subquery()
        )
        return [
            execution(r)
            for r in self.session.scalars(
                select(ScheduleExecutionRow)
                .join(heads, heads.c.id == ScheduleExecutionRow.id)
                .where(heads.c.position == 1)
                .order_by(ScheduleExecutionRow.scheduled_for_utc, ScheduleExecutionRow.id)
                .limit(limit)
            )
        ]

    def coordinate(self, entity: ScheduleExecution) -> bool:
        # All policies take the same TARGET lock, including SCHEDULE-scoped/ALLOW dispatches.
        key = f"{entity.project_id}:{entity.target_kind}:{entity.target_name}"
        id = int.from_bytes(sha256(key.encode()).digest()[:8], "big", signed=True)
        return bool(self.session.scalar(text("SELECT pg_try_advisory_xact_lock(:id)"), {"id": id}))

    def _scope(self, entity: ScheduleExecution) -> ColumnElement[bool]:
        if entity.concurrency_scope == ConcurrencyScope.SCHEDULE:
            return ScheduleExecutionRow.schedule_id == entity.schedule_id
        return cast(
            ColumnElement[bool],
            (
                (ScheduleExecutionRow.project_id == entity.project_id)
                & (ScheduleExecutionRow.spec["target_kind"].astext == entity.target_kind)
                & (ScheduleExecutionRow.spec["target_name"].astext == entity.target_name)
            ),
        )

    def active(self, entity: ScheduleExecution) -> bool:
        query = (
            select(ScheduleExecutionRow.id)
            .outerjoin(PipelineRunRow, PipelineRunRow.id == ScheduleExecutionRow.pipeline_run_id)
            .outerjoin(RunRow, RunRow.id == ScheduleExecutionRow.job_run_id)
            .where(
                self._scope(entity),
                ScheduleExecutionRow.status == "DISPATCHED",
                or_(PipelineRunRow.status.in_(ACTIVE), RunRow.status.in_(ACTIVE)),
            )
            .limit(1)
        )
        return self.session.scalar(query) is not None

    def queue_size(self, entity: ScheduleExecution) -> int:
        return (
            self.session.scalar(
                select(func.count())
                .select_from(ScheduleExecutionRow)
                .where(self._scope(entity), ScheduleExecutionRow.status == "QUEUED")
            )
            or 0
        )

    def older_queued(self, entity: ScheduleExecution) -> bool:
        return (
            self.session.scalar(
                select(ScheduleExecutionRow.id)
                .join(ScheduleRow, ScheduleRow.id == ScheduleExecutionRow.schedule_id)
                .where(
                    self._scope(entity),
                    ~ScheduleRow.paused,
                    ScheduleExecutionRow.status == "QUEUED",
                    or_(
                        ScheduleExecutionRow.scheduled_for_utc < entity.scheduled_for_utc,
                        (ScheduleExecutionRow.scheduled_for_utc == entity.scheduled_for_utc)
                        & (ScheduleExecutionRow.id < entity.id),
                    ),
                )
                .limit(1)
            )
            is not None
        )
