"""Structured logs that carry the trace they belong to.

Every record gets `service`, and `trace_id` / `span_id` whenever there is one, so a log line
found in the cluster's logs can be opened as a trace (and the other way round). Standard
library records (uvicorn, kubernetes, alembic) go through the same pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog

from controlplane.application.context import current_traceparent


def add_trace_context(_: Any, __: str, event: dict[str, Any]) -> dict[str, Any]:
    parts = (current_traceparent() or "").split("-")
    if len(parts) == 4 and len(parts[1]) == 32:
        event.setdefault("trace_id", parts[1])
        if len(parts[2]) == 16:
            event.setdefault("span_id", parts[2])
    return event


def configure_logging(service: str, *, json: bool = True, level: str = "INFO") -> None:
    def add_service(_: Any, __: str, event: dict[str, Any]) -> dict[str, Any]:
        event.setdefault("service", service)
        return event

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        add_service,
        add_trace_context,
    ]
    renderer: Any = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    handler = logging.StreamHandler()
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
