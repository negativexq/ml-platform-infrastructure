"""Where a piece of work came from, as a W3C `traceparent` string.

Stdlib only, so the domain and application layers stay free of OpenTelemetry. Two sources,
in this order:

1. the **origin** bound by a reconciler: the trace of the request that created the entity
   it is working on, so the whole lifecycle (request -> submit -> progress -> result) can be
   followed as one trace even though it spans minutes and several processes;
2. the **provider**, installed by the observability package at start-up: the span that is
   active right now (an API request).

With neither (tests, no telemetry configured) the answer is None and nothing changes.
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar

_origin: ContextVar[str | None] = ContextVar("mlp_origin_traceparent", default=None)
_provider: Callable[[], str | None] = lambda: None  # noqa: E731


def set_traceparent_provider(provider: Callable[[], str | None]) -> None:
    global _provider
    _provider = provider


def current_traceparent() -> str | None:
    """The traceparent to store on an entity being created or re-targeted right now."""
    return _origin.get() or _provider()


def bind_origin(traceparent: str | None) -> None:
    """Called by a reconciler for the entity it is about to work on."""
    _origin.set(traceparent)


def clear_origin() -> None:
    _origin.set(None)


def bound_origin() -> str | None:
    return _origin.get()


def trace_id_of(traceparent: str | None) -> str | None:
    """`00-<32 hex trace id>-<16 hex span id>-<flags>` -> the trace id, or None if malformed."""
    if not traceparent:
        return None
    parts = traceparent.split("-")
    if len(parts) == 4 and len(parts[1]) == 32 and parts[1] != "0" * 32:
        return parts[1]
    return None
