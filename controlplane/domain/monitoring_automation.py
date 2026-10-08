"""Dataset publication intents and immutable monitoring occurrences."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class DatasetPublishedEvent:
    id: UUID
    project_id: UUID
    dataset_id: UUID
    created_at: datetime
    processed_at: datetime | None = None


@dataclass(frozen=True)
class MonitoringRule:
    id: UUID
    project_id: UUID
    name: str
    model_id: UUID
    model_version_id: UUID
    reference_dataset_id: UUID
    observed_dataset_name: str
    feedback_dataset_name: str | None
    options: dict[str, Any]
    image: str
    feedback_deadline_seconds: int
    enabled: bool
    revision: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class MonitoringExecution:
    id: UUID
    project_id: UUID
    rule_id: UUID
    event_id: UUID
    observed_dataset_id: UUID
    snapshot: dict[str, Any]
    status: str
    reason: str | None
    job_definition_id: UUID | None
    job_run_id: UUID | None
    deadline_at: datetime
    next_attempt_at: datetime
    created_at: datetime
    updated_at: datetime
