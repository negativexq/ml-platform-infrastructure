"""Gateway telemetry profiles; latency sampling never samples usage counters."""

import os
import random
from dataclasses import dataclass


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value.lower() not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value.lower() == "true"


def _rate(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not 0 <= value <= 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return value


def sample_latency(rate: float) -> bool:
    return rate == 1 or (rate > 0 and random.random() < rate)


@dataclass(frozen=True)
class GatewayTelemetryProfile:
    name: str
    sql_tracing: bool
    latency_sample_rate: float
    trace_sample_rate: float
    request_caller_label: bool
    native_http_metrics: bool
    operation_spans: bool
    export_interval_ms: int

    @classmethod
    def from_env(cls) -> "GatewayTelemetryProfile":
        name = os.getenv("CP_GATEWAY_OBSERVABILITY_PROFILE", "normal")
        if name not in {"normal", "diagnostic", "acceptance"}:
            raise ValueError("CP_GATEWAY_OBSERVABILITY_PROFILE: normal, diagnostic or acceptance")
        diagnostic = name == "diagnostic"
        interval = int(
            os.getenv(
                "CP_GATEWAY_METRIC_EXPORT_INTERVAL_MS",
                os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "1000" if name != "normal" else "30000"),
            )
        )
        if interval <= 0:
            raise ValueError("CP_GATEWAY_METRIC_EXPORT_INTERVAL_MS must be positive")
        return cls(
            name=name,
            sql_tracing=_boolean("CP_GATEWAY_SQL_TRACING", diagnostic),
            latency_sample_rate=_rate(
                "CP_GATEWAY_LATENCY_SAMPLE_RATE", 0.2 if name == "normal" else 1
            ),
            trace_sample_rate=_rate("CP_GATEWAY_TRACE_SAMPLE_RATE", 1 if diagnostic else 0.01),
            request_caller_label=_boolean("CP_GATEWAY_REQUEST_CALLER_LABEL", False),
            native_http_metrics=_boolean("CP_GATEWAY_HTTP_METRICS", diagnostic),
            operation_spans=_boolean("CP_GATEWAY_HTTP_OPERATION_SPANS", diagnostic),
            export_interval_ms=interval,
        )
