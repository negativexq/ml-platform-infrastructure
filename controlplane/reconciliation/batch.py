"""A failed entity is retried next pass without starving the rest of the batch."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from uuid import UUID

from controlplane.domain.errors import Conflict

log = logging.getLogger(__name__)


class ReconcileBackoff:
    """Process-local, capped exponential retry delays; success/conflict clears failure state."""

    def __init__(
        self,
        now: Callable[[], float] = time.monotonic,
        base_seconds: float = 5,
        max_seconds: float = 300,
    ) -> None:
        if not 0 < base_seconds <= max_seconds:
            raise ValueError("retry delays require 0 < base <= maximum")
        self._now = now
        self._base, self._max = base_seconds, max_seconds
        self._failures: dict[UUID, tuple[int, float]] = {}

    def ready(self, entity_id: UUID) -> bool:
        return self._now() >= self._failures.get(entity_id, (0, 0))[1]

    def failed(self, entity_id: UUID) -> None:
        count = self._failures.get(entity_id, (0, 0))[0] + 1
        delay = min(self._max, self._base * 2 ** min(count - 1, 20))
        self._failures[entity_id] = (count, self._now() + delay)

    def clear(self, entity_id: UUID) -> None:
        self._failures.pop(entity_id, None)

    def prune(self, active: set[UUID]) -> None:
        for entity_id in self._failures.keys() - active:
            del self._failures[entity_id]


def reconcile_batch[T](
    ids: Iterable[UUID],
    reconcile: Callable[[UUID], T],
    kind: str,
    backoff: ReconcileBackoff | None = None,
) -> list[T]:
    ids = list(ids)
    if backoff is not None:
        backoff.prune(set(ids))
    results = []
    for entity_id in ids:
        if backoff is not None and not backoff.ready(entity_id):
            continue
        try:
            results.append(reconcile(entity_id))
            if backoff is not None:
                backoff.clear(entity_id)
        except Conflict:
            if backoff is not None:
                backoff.clear(entity_id)
            continue  # another writer moved it; next pass picks it up
        except Exception:  # noqa: BLE001 - isolate provider and persistence failures per entity
            if backoff is not None:
                backoff.failed(entity_id)
            log.exception("reconcile failed: kind=%s entity_id=%s", kind, entity_id)
    return results
