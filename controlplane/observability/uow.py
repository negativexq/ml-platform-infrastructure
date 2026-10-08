"""A unit of work that reports what it changed, after it has actually been committed."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from types import TracebackType
from typing import Any, Self, cast
from uuid import UUID

from controlplane.application.context import bound_origin, current_traceparent, trace_id_of
from controlplane.application.ports import AuditLog, UnitOfWork
from controlplane.application.projects import UnitOfWorkFactory
from controlplane.domain.audit import AuditEvent
from controlplane.observability.metrics import record_transition
from controlplane.observability.tracing import emit_lifecycle_span

log = logging.getLogger(__name__)


class _ObservedAudit:
    def __init__(self, inner: AuditLog, pending: list[tuple[AuditEvent, str | None]]) -> None:
        self._inner = inner
        self._pending = pending

    def latest(
        self, *, project_id: UUID, entity_type: str, entity_id: UUID, actions: Sequence[str]
    ) -> AuditEvent | None:
        return self._inner.latest(
            project_id=project_id, entity_type=entity_type, entity_id=entity_id, actions=actions
        )

    def record(self, event: AuditEvent) -> None:
        origin = bound_origin()
        if event.trace_id is None:
            event = replace(event, trace_id=trace_id_of(origin or current_traceparent()))
        self._inner.record(event)
        self._pending.append((event, origin))

    def list(self, *, project_id: UUID | None = None, entity_id: UUID | None = None) -> Any:
        return self._inner.list(project_id=project_id, entity_id=entity_id)


class ObservedUnitOfWork:
    """Wraps a UnitOfWork: stamps each audit event with its trace id, and on a successful
    commit emits one lifecycle span and one transition metric per event. A transaction that
    rolls back reports nothing: no span for a change that did not happen."""

    def __init__(self, inner: UnitOfWork) -> None:
        self._inner = inner
        self._pending: list[tuple[AuditEvent, str | None]] = []

    def __enter__(self) -> Self:
        self._inner.__enter__()
        self._pending = []
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._inner.__exit__(exc_type, exc, tb)

    @property
    def audit(self) -> AuditLog:
        return _ObservedAudit(self._inner.audit, self._pending)

    def commit(self) -> None:
        self._inner.commit()
        pending, self._pending = self._pending, []
        for event, origin in pending:
            try:  # observability must never break the work it observes
                record_transition(event.entity_type, event.action)
                emit_lifecycle_span(event, origin)
            except Exception:  # noqa: BLE001
                log.exception("could not report %s", event.action)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def observed_uow_factory(factory: UnitOfWorkFactory) -> UnitOfWorkFactory:
    return lambda: cast(UnitOfWork, ObservedUnitOfWork(factory()))
