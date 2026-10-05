from __future__ import annotations

import logging
from uuid import UUID

import pytest

from controlplane.domain.errors import Conflict
from controlplane.reconciliation.batch import ReconcileBackoff, reconcile_batch


def test_error_logs_identity_and_later_entities_still_run(caplog: pytest.LogCaptureFixture) -> None:
    broken, healthy = UUID(int=1), UUID(int=2)
    calls = []

    def reconcile(entity_id: UUID) -> UUID:
        calls.append(entity_id)
        if entity_id == broken:
            raise ConnectionError("provider down")
        return entity_id

    with caplog.at_level(logging.ERROR):
        assert reconcile_batch([broken, healthy], reconcile, "runs") == [healthy]
    assert calls == [broken, healthy]
    assert str(broken) in caplog.text and "provider down" in caplog.text


def test_conflict_is_retryable_without_an_error_log(caplog: pytest.LogCaptureFixture) -> None:
    entity_id = UUID(int=1)

    def reconcile(_: UUID) -> None:
        raise Conflict("another writer")

    assert reconcile_batch([entity_id], reconcile, "runs") == []
    assert not caplog.records


def test_retry_delay_doubles_caps_and_resets_after_success() -> None:
    now = 0.0
    entity = UUID(int=1)
    backoff = ReconcileBackoff(lambda: now, base_seconds=5, max_seconds=10)
    backoff.failed(entity)
    assert not backoff.ready(entity)
    now = 5
    assert backoff.ready(entity)
    backoff.failed(entity)
    now = 14
    assert not backoff.ready(entity)
    now = 15
    assert backoff.ready(entity)
    backoff.failed(entity)
    now = 24
    assert not backoff.ready(entity)
    now = 25
    assert reconcile_batch([entity], lambda item: item, "runs", backoff) == [entity]
    backoff.failed(entity)
    now = 30
    assert backoff.ready(entity)  # reset to base delay, not maximum
