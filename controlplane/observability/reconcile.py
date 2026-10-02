"""Reconciler instrumentation: metrics for every entity and every pass, and the origin trace
bound while an entity is being reconciled so the changes it makes join the right trace."""

from __future__ import annotations

import logging
from collections.abc import Callable
from time import perf_counter
from typing import Any
from uuid import UUID

from controlplane.application.context import bind_origin, clear_origin
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import UnitOfWorkFactory
from controlplane.domain.errors import Conflict
from controlplane.observability.metrics import record_pass, record_reconcile

log = logging.getLogger(__name__)

# How to read the stored traceparent of the entity a reconciler is about to work on.
TraceparentOf = Callable[[UnitOfWork, UUID], str | None]


def _changed(result: Any) -> bool:
    """Did the pass do something? Every reconciler's result type says so a little differently."""
    if getattr(result, "before", None) != getattr(result, "after", None):
        return True
    return bool(
        getattr(result, "changed", None)
        or getattr(result, "applied", False)
        or getattr(result, "synced", None)
    )


def instrument_reconciler(
    reconciler: Any,
    kind: str,
    uow_factory: UnitOfWorkFactory,
    traceparent_of: TraceparentOf | None = None,
) -> Any:
    """Patch `reconcile` and `reconcile_all` on this instance (reconcile_all calls
    `self.reconcile`, so per-entity metrics come for free). Returns the same object."""
    original_one = reconciler.reconcile
    original_all = reconciler.reconcile_all

    def reconcile(entity_id: UUID) -> Any:
        origin = None
        if traceparent_of is not None:
            try:
                with uow_factory() as uow:
                    origin = traceparent_of(uow, entity_id)
            except Exception:  # noqa: BLE001 - never let a lookup stop the reconcile
                log.debug("no origin trace for %s", entity_id, exc_info=True)
        bind_origin(origin)
        started = perf_counter()
        try:
            result = original_one(entity_id)
        except Conflict:
            record_reconcile(kind, "conflict", perf_counter() - started)
            raise
        except Exception:
            record_reconcile(kind, "error", perf_counter() - started)
            raise
        finally:
            clear_origin()
        record_reconcile(
            kind, "changed" if _changed(result) else "unchanged", perf_counter() - started
        )
        return result

    def reconcile_all() -> Any:
        try:
            return original_all()
        finally:
            clear_origin()
            record_pass(kind)

    reconciler.reconcile = reconcile
    reconciler.reconcile_all = reconcile_all
    return reconciler
