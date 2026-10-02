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
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from opentelemetry import metrics
from opentelemetry.metrics import Counter, Histogram

_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0]


@dataclass(frozen=True)
class _Instruments:
    runs: Counter
    duration: Histogram
    passes: Counter
    transitions: Counter
    calls: Counter
    call_duration: Histogram


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
