"""Shared native FastAPI telemetry; composition roots own the exporters."""

from collections.abc import MutableMapping
from typing import Any

from fastapi.telemetry import TelemetryConfig


def _exclude(scope: MutableMapping[str, Any]) -> bool:
    path = scope.get("path", "")
    return bool(path in {"/healthz", "/readyz"} or path.startswith("/ui"))


def telemetry_config(extra: TelemetryConfig | None = None) -> TelemetryConfig:
    config: TelemetryConfig = {"exclude": _exclude, "auto_configure": False, "logs": False}
    config.update(extra or {})
    return config
