from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from controlplane.domain.ids import new_id


@dataclass(frozen=True, slots=True, kw_only=True)
class AuditEvent:
    """Append-only record of a lifecycle change, written in the same
    transaction as the change itself."""

    id: UUID = field(default_factory=new_id)
    occurred_at: datetime
    actor: str
    action: str  # e.g. "project.created"
    entity_type: str
    entity_id: UUID
    project_id: UUID | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    # The trace this change belongs to, so an audit row can be opened as a trace.
    trace_id: str | None = None
