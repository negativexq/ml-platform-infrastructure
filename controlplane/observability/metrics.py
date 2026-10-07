"""The control plane's own metrics (OpenTelemetry), next to FastAPI's native HTTP metrics.

Instruments are created on first use, so importing this module never touches a provider.
With no provider configured every call below is a no-op.

Names as Prometheus sees them (the collector's Prometheus exporter appends the unit and
`_total`):

  mlp_reconcile_runs_total{reconciler,outcome}      outcome: changed | unchanged | conflict | error
  mlp_reconcile_duration_seconds{reconciler}        histogram, one observation per entity
  mlp_reconcile_passes_total{reconciler}            a heartbeat: increments once per full pass
  mlp_state_transitions_total{entity_type,action}   every audit event, i.e. every real state change
  mlp_provider_calls_total{provider,operation,outcome}   outcome: ok | not_found | error
  mlp_provider_duration_seconds{provider,operation}
  mlp_gateway_requests_total{project,endpoint,code}   every public call, by HTTP status
  mlp_gateway_units_total{project,endpoint,caller,unit}      what quotas count (requests, tokens)
  mlp_gateway_duration_seconds{project,endpoint}             whole call, streaming included
  mlp_gateway_tokens_total{project,endpoint,caller,direction}  LLMs: prompt | completion
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Any

from opentelemetry import metrics
from opentelemetry.metrics import Counter, Histogram

from controlplane.observability.profile import sample_latency

_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0]


@dataclass(frozen=True)
class _Instruments:
    runs: Counter
    duration: Histogram
    passes: Counter
    transitions: Counter
    calls: Counter
    call_duration: Histogram
    gateway_requests: Counter
    gateway_usage_requests: Counter
    gateway_units: Counter
    gateway_duration: Histogram
    gateway_tokens: Counter


@cache
def _instruments() -> _Instruments:
    meter = metrics.get_meter("controlplane")
    return _Instruments(
        runs=meter.create_counter(
            "mlp.reconcile.runs", unit="{reconcile}", description="Reconciliations by outcome"
        ),
        duration=meter.create_histogram(
            "mlp.reconcile.duration",
            unit="s",
            description="Time spent reconciling one entity",
            explicit_bucket_boundaries_advisory=_BUCKETS,
        ),
        passes=meter.create_counter(
            "mlp.reconcile.passes", unit="{pass}", description="Completed reconcile passes"
        ),
        transitions=meter.create_counter(
            "mlp.state.transitions", unit="{transition}", description="State changes (audit events)"
        ),
        calls=meter.create_counter(
            "mlp.provider.calls", unit="{call}", description="Calls to external systems"
        ),
        call_duration=meter.create_histogram(
            "mlp.provider.duration",
            unit="s",
            description="Latency of calls to external systems",
            explicit_bucket_boundaries_advisory=_BUCKETS,
        ),
        gateway_requests=meter.create_counter(
            "mlp.gateway.requests", unit="{request}", description="Calls through the gateway"
        ),
        gateway_usage_requests=meter.create_counter(
            "mlp.gateway.usage.requests",
            unit="{request}",
            description="Per-caller rejection/error usage; operational requests omit caller",
        ),
        gateway_units=meter.create_counter(
            "mlp.gateway.units", unit="{unit}", description="Quota units used through the gateway"
        ),
        gateway_duration=meter.create_histogram(
            "mlp.gateway.duration",
            unit="s",
            description="Time to serve a call through the gateway",
            explicit_bucket_boundaries_advisory=_BUCKETS,
        ),
        gateway_tokens=meter.create_counter(
            "mlp.gateway.tokens", unit="{token}", description="LLM tokens through the gateway"
        ),
    )


def record_reconcile(reconciler: str, outcome: str, seconds: float) -> None:
    i = _instruments()
    i.runs.add(1, {"reconciler": reconciler, "outcome": outcome})
    i.duration.record(seconds, {"reconciler": reconciler})


def record_pass(reconciler: str) -> None:
    _instruments().passes.add(1, {"reconciler": reconciler})


def record_transition(entity_type: str, action: str) -> None:
    _instruments().transitions.add(1, {"entity_type": entity_type, "action": action})


def record_provider_call(provider: str, operation: str, outcome: str, seconds: float) -> None:
    i = _instruments()
    attrs = {"provider": provider, "operation": operation}
    i.calls.add(1, {**attrs, "outcome": outcome})
    i.call_duration.record(seconds, attrs)


def record_gateway_call(
    project: str,
    endpoint: str,
    caller: str,
    code: int,
    units: float,
    unit: str,
    seconds: float,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    *,
    latency_sample_rate: float = 1,
    request_caller_label: bool = False,
) -> None:
    i = _instruments()
    where = {"project": project, "endpoint": endpoint}
    request_attrs = {**where, "code": str(code)}
    if request_caller_label:
        request_attrs["caller"] = caller
    i.gateway_requests.add(1, request_attrs)
    if code >= 400:
        i.gateway_usage_requests.add(1, {**where, "caller": caller, "code": str(code)})
    if units:
        i.gateway_units.add(units, {**where, "caller": caller, "unit": unit})
    for direction, tokens in (("prompt", prompt_tokens), ("completion", completion_tokens)):
        if tokens:
            i.gateway_tokens.add(tokens, {**where, "caller": caller, "direction": direction})
    if sample_latency(latency_sample_rate):
        i.gateway_duration.record(seconds, where)


class GatewayUsageMetrics:
    """The gateway's UsageRecorder: usage as metrics, which Prometheus keeps and the
    control plane reads back for the usage panel."""

    def __init__(
        self, *, latency_sample_rate: float = 1, request_caller_label: bool = False
    ) -> None:
        self.sample_rate = latency_sample_rate
        self.request_caller_label = request_caller_label

    def record(self, call: Any) -> None:  # a controlplane.application.gateway.CallRecord
        record_gateway_call(
            call.project,
            call.endpoint,
            call.caller,
            call.status,
            call.units,
            call.unit,
            call.seconds,
            call.prompt_tokens,
            call.completion_tokens,
            latency_sample_rate=self.sample_rate,
            request_caller_label=self.request_caller_label,
        )


class GatewayLimiterMetrics:
    def __init__(self, *, latency_sample_rate: float = 1) -> None:
        self.sample_rate = latency_sample_rate
        self._duration = metrics.get_meter("controlplane").create_histogram(
            "mlp.gateway.limiter.duration",
            unit="s",
            description="Limiter transaction latency",
            explicit_bucket_boundaries_advisory=[
                0.001,
                0.005,
                0.01,
                0.025,
                0.05,
                0.1,
                0.25,
                0.5,
                1,
                2,
                3,
                6,
            ],
        )

    def record(self, operation: str, outcome: str, seconds: float) -> None:
        if sample_latency(self.sample_rate):
            self._duration.record(seconds, {"operation": operation, "outcome": outcome})
