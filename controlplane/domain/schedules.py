"""Durable scheduling intent. Queued occurrences deliberately have no platform run."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from controlplane.domain.ids import new_id


class TargetKind(StrEnum):
    PIPELINE = "PIPELINE"
    JOB = "JOB"


class VersionPolicy(StrEnum):
    PINNED = "PINNED"
    LATEST = "LATEST"


class ConcurrencyPolicy(StrEnum):
    ALLOW = "ALLOW"
    FORBID = "FORBID"
    QUEUE = "QUEUE"


class ConcurrencyScope(StrEnum):
    SCHEDULE = "SCHEDULE"
    TARGET = "TARGET"


class MissedRunPolicy(StrEnum):
    SKIP = "SKIP"
    CATCH_UP = "CATCH_UP"


class ExecutionStatus(StrEnum):
    QUEUED = "QUEUED"
    DISPATCHED = "DISPATCHED"
    SKIPPED = "SKIPPED"
    MISSED = "MISSED"


@dataclass(frozen=True, kw_only=True)
class Schedule:
    id: UUID = field(default_factory=new_id)
    project_id: UUID
    name: str
    target_kind: TargetKind
    target_name: str
    cron: str
    timezone: str
    version_policy: VersionPolicy = VersionPolicy.PINNED
    version: int | None = None
    concurrency_policy: ConcurrencyPolicy = ConcurrencyPolicy.FORBID
    concurrency_scope: ConcurrencyScope = ConcurrencyScope.TARGET
    missed_run_policy: MissedRunPolicy = MissedRunPolicy.SKIP
    deadline_seconds: int = 300
    queue_ttl_seconds: int = 86400
    max_queue_size: int = 100
    parameters: Mapping[str, Any] = field(default_factory=dict)
    parameter_bindings: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: int = 3600
    paused: bool = False
    revision: int = 1
    next_run_at: datetime
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, kw_only=True)
class ScheduleExecution:
    id: UUID = field(default_factory=new_id)
    schedule_id: UUID
    project_id: UUID
    scheduled_for_utc: datetime
    schedule_revision: int
    resolved_definition_id: UUID | None
    target_kind: TargetKind
    target_name: str
    concurrency_policy: ConcurrencyPolicy
    concurrency_scope: ConcurrencyScope
    timeout_seconds: int
    expires_at: datetime
    parameters: Mapping[str, Any] = field(default_factory=dict)
    status: ExecutionStatus = ExecutionStatus.QUEUED
    pipeline_run_id: UUID | None = None
    job_run_id: UUID | None = None
    reason: str | None = None
    created_at: datetime
    updated_at: datetime
