"""MetricsProvider backed by Prometheus, reading Knative/KServe per-revision metrics.

Judging a canary needs the canary's own numbers, so every query is scoped to one
backend revision (`revision_name`), never the whole service.

Live healthy/error-only canary evidence is recorded in docs/evidence/live-2026-10-06.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from controlplane.application.providers import (
    EndpointUsageSeries,
    MetricsPoint,
    PlatformSignal,
    RevisionMetrics,
    Sample,
    UsagePoint,
)

_SAFE_LABEL = re.compile(r"^[A-Za-z0-9._-]+$")
TIMEOUT_SECONDS = 5


class PrometheusMetricsProvider:
    def __init__(self, base_url: str, window: str = "2m") -> None:
        self._base = base_url.rstrip("/")
        self._window = window

    def revision_metrics(
        self, endpoint_ref: str, revision: int, backend_revision: str | None = None
    ) -> RevisionMetrics:
        if backend_revision is None:
            return RevisionMetrics(None, None, None, None)  # nothing to scope the query to
        namespace, _, _ = endpoint_ref.partition("/")
        for value in (namespace, backend_revision):
            if not _SAFE_LABEL.match(value):
                raise ValueError(f"unsafe label value {value!r}")
        sel = f'namespace_name="{namespace}",revision_name="{backend_revision}"'
        w = self._window
        # Use one observation instant: moving counters queried at different times
        # can otherwise produce an error ratio above 100% or cross a gate boundary.
        at = datetime.now(UTC).timestamp()
        requests = self._scalar(f"sum(increase(revision_request_count{{{sel}}}[{w}]))", at=at)
        errors = self._scalar(
            f'sum(increase(revision_request_count{{{sel},response_code_class="5xx"}}[{w}]))',
            at=at,
        )
        rps = self._scalar(f"sum(rate(revision_request_count{{{sel}}}[{w}]))", at=at)
        buckets = f"sum(rate(revision_request_latencies_bucket{{{sel}}}[{w}])) by (le)"
        p95 = self._scalar(f"histogram_quantile(0.95, {buckets})", at=at)
        error_rate = (errors or 0.0) / requests if requests else None
        return RevisionMetrics(
            p95_latency_ms=p95, error_rate=error_rate, requests_per_second=rps, requests=requests
        )

    def revision_history(
        self,
        endpoint_ref: str,
        revision: int,
        backend_revision: str | None,
        *,
        start: datetime,
        end: datetime,
        step_seconds: int,
    ) -> Sequence[MetricsPoint]:
        """`query_range` for the same three numbers, joined on their timestamps."""
        if backend_revision is None:
            return []
        sel = self._selector(endpoint_ref, backend_revision)
        w = self._window
        everything = f"sum(rate(revision_request_count{{{sel}}}[{w}]))"
        failed = f'sum(rate(revision_request_count{{{sel},response_code_class="5xx"}}[{w}]))'
        buckets = f"sum(rate(revision_request_latencies_bucket{{{sel}}}[{w}])) by (le)"
        window = {"start": start.timestamp(), "end": end.timestamp(), "step": f"{step_seconds}s"}
        p95 = self._range(f"histogram_quantile(0.95, {buckets})", window)
        # 0/0 (no traffic) is NaN in Prometheus, which _range turns into a gap
        errors = self._range(f"({failed} or {everything} * 0) / {everything}", window)
        rps = self._range(everything, window)
        return [
            MetricsPoint(
                at=datetime.fromtimestamp(ts, UTC),
                p95_latency_ms=p95.get(ts),
                error_rate=errors.get(ts),
                requests_per_second=rps.get(ts),
            )
            for ts in sorted(set(p95) | set(errors) | set(rps))
        ]

    @staticmethod
    def _selector(endpoint_ref: str, backend_revision: str) -> str:
        namespace, _, _ = endpoint_ref.partition("/")
        for value in (namespace, backend_revision):
            if not _SAFE_LABEL.match(value):
                raise ValueError(f"unsafe label value {value!r}")
        return f'namespace_name="{namespace}",revision_name="{backend_revision}"'

    def _range(self, query: str, window: dict[str, Any]) -> dict[float, float]:
        params = urllib.parse.urlencode({"query": query, **window})
        body = self._get(f"{self._base}/api/v1/query_range?{params}")
        results = body.get("data", {}).get("result", [])
        if not results:
            return {}
        out: dict[float, float] = {}
        for ts, raw in results[0]["values"]:
            value = float(raw)
            if value == value:  # drop NaN: no data at that moment
                out[float(ts)] = value
        return out

    def _get(self, url: str) -> dict[str, Any]:
        return _get(url)

    def _scalar(self, query: str, *, at: float) -> float | None:
        url = f"{self._base}/api/v1/query?{urllib.parse.urlencode({'query': query, 'time': at})}"
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
                body: dict[str, Any] = json.load(response)
        except urllib.error.URLError as exc:
            raise ConnectionError(f"prometheus query failed: {exc}") from exc
        results = body.get("data", {}).get("result", [])
        if not results:
            return None
        value = float(results[0]["value"][1])
        return None if value != value else value  # NaN (e.g. quantile of nothing) -> no data


API_JOB = "mlp-controlplane-api"
RECONCILER_JOB = "mlp-controlplane-reconciler"


def _ratio(failed: str, everything: str) -> str:
    # 0/0 (nothing happened) is NaN, which becomes a gap; no error series at all is 0
    return f"(({failed}) or ({everything}) * 0) / ({everything})"


def platform_queries(window: str) -> dict[PlatformSignal, tuple[str, str]]:
    """PromQL per signal and the label it is grouped by ("" for none). The same names the
    alert rules and the Grafana dashboard use."""
    w = window
    api = f'job="{API_JOB}"'
    rec = f'job="{RECONCILER_JOB}"'
    http = "http_server_request_duration_seconds"
    runs = "mlp_reconcile_runs_total"
    calls = "mlp_provider_calls_total"
    gateway = "mlp_gateway_requests_total"
    return {
        PlatformSignal.API_REQUESTS: (f"sum(rate({http}_count{{{api}}}[{w}]))", ""),
        PlatformSignal.API_ERRORS: (
            _ratio(
                f'sum(rate({http}_count{{{api},http_response_status_code=~"5.."}}[{w}]))',
                f"sum(rate({http}_count{{{api}}}[{w}]))",
            ),
            "",
        ),
        PlatformSignal.API_LATENCY: (
            f"1000 * histogram_quantile(0.95, sum by (le) (rate({http}_bucket{{{api}}}[{w}])))",
            "",
        ),
        PlatformSignal.RECONCILE_PASSES: (
            f"60 * sum by (reconciler) (rate(mlp_reconcile_passes_total{{{rec}}}[{w}]))",
            "reconciler",
        ),
        PlatformSignal.RECONCILE_ERRORS: (
            _ratio(
                f'sum by (reconciler) (rate({runs}{{{rec},outcome="error"}}[{w}]))',
                f"sum by (reconciler) (rate({runs}{{{rec}}}[{w}]))",
            ),
            "reconciler",
        ),
        PlatformSignal.PROVIDER_ERRORS: (
            _ratio(
                f'sum by (provider) (rate({calls}{{outcome="error"}}[{w}]))',
                f"sum by (provider) (rate({calls}[{w}]))",
            ),
            "provider",
        ),
        PlatformSignal.PROVIDER_LATENCY: (
            "1000 * histogram_quantile(0.95, sum by (le, provider) "
            f"(rate(mlp_provider_duration_seconds_bucket[{w}])))",
            "provider",
        ),
        PlatformSignal.TRANSITIONS: (
            f"60 * sum by (entity_type) (rate(mlp_state_transitions_total[{w}]))",
            "entity_type",
        ),
        PlatformSignal.GATEWAY_REQUESTS: (f"sum(rate({gateway}[{w}]))", ""),
        PlatformSignal.GATEWAY_ERRORS: (
            _ratio(
                f'sum(rate({gateway}{{code=~"5.."}}[{w}]))',
                f"sum(rate({gateway}[{w}]))",
            ),
            "",
        ),
        PlatformSignal.GATEWAY_TOKENS: (
            f"60 * sum by (direction) (rate(mlp_gateway_tokens_total[{w}]))",
            "direction",
        ),
        PlatformSignal.GATEWAY_LATENCY: (
            "1000 * histogram_quantile(0.95, sum by (le) "
            f"(rate(mlp_gateway_duration_seconds_bucket[{w}])))",
            "",
        ),
    }


class PrometheusPlatformTelemetry:
    """The control plane's own metrics (see controlplane/observability/metrics.py), read back
    for the Monitor page."""

    def __init__(self, base_url: str, window: str = "5m") -> None:
        self._base = base_url.rstrip("/")
        self._queries = platform_queries(window)

    def platform_series(
        self, signal: PlatformSignal, *, start: datetime, end: datetime, step_seconds: int
    ) -> Mapping[str, Sequence[Sample]]:
        query, label = self._queries[signal]
        params = urllib.parse.urlencode(
            {
                "query": query,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": f"{step_seconds}s",
            }
        )
        body = _get(f"{self._base}/api/v1/query_range?{params}")
        out: dict[str, list[Sample]] = {}
        for result in body.get("data", {}).get("result", []):
            group = result.get("metric", {}).get(label, "") if label else ""
            points = [
                Sample(datetime.fromtimestamp(float(ts), UTC), float(raw))
                for ts, raw in result["values"]
                if float(raw) == float(raw)  # NaN: nothing happened, a gap
            ]
            if points:
                out.setdefault(group, []).extend(points)
        return {group: sorted(points, key=lambda s: s.at) for group, points in sorted(out.items())}


def _get(url: str) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            body: dict[str, Any] = json.load(response)
    except urllib.error.URLError as exc:
        raise ConnectionError(f"prometheus query failed: {exc}") from exc
    return body


class PrometheusUsage:
    """Gateway usage per caller, read back from the gateway's own metrics."""

    def __init__(self, base_url: str, window: str = "5m") -> None:
        self._base = base_url.rstrip("/")
        self._window = window

    def endpoint_usage(
        self, project: str, endpoint: str, *, start: datetime, end: datetime, step_seconds: int
    ) -> EndpointUsageSeries:
        for value in (project, endpoint):
            if not _SAFE_LABEL.match(value):
                raise ValueError(f"unsafe label value {value!r}")
        sel = f'project="{project}",endpoint="{endpoint}"'
        w = self._window
        requests = "mlp_gateway_usage_requests_total"
        window = {"start": start.timestamp(), "end": end.timestamp(), "step": f"{step_seconds}s"}
        units = self._by_caller(
            f"60 * sum by (caller) (rate(mlp_gateway_units_total{{{sel}}}[{w}]))", window
        )
        rejected = self._by_caller(
            f'60 * sum by (caller) (rate({requests}{{{sel},code=~"4.."}}[{w}]) '
            f'or rate(mlp_gateway_requests_total{{{sel},caller!="",code=~"4.."}}[{w}]))',
            window,
        )
        errors = self._by_caller(
            f'60 * sum by (caller) (rate({requests}{{{sel},code=~"5.."}}[{w}]) '
            f'or rate(mlp_gateway_requests_total{{{sel},caller!="",code=~"5.."}}[{w}]))',
            window,
        )
        callers: dict[str, list[UsagePoint]] = {}
        for caller in sorted(set(units) | set(rejected) | set(errors)):
            if caller in ("(anonymous)",):
                continue
            moments = sorted(
                set(units.get(caller, {}))
                | set(rejected.get(caller, {}))
                | set(errors.get(caller, {}))
            )
            callers[caller] = [
                UsagePoint(
                    at=datetime.fromtimestamp(ts, UTC),
                    units=units.get(caller, {}).get(ts, 0.0),
                    rejected=rejected.get(caller, {}).get(ts, 0.0),
                    errors=errors.get(caller, {}).get(ts, 0.0),
                )
                for ts in moments
            ]
        buckets = f"sum by (le) (rate(mlp_gateway_duration_seconds_bucket{{{sel}}}[{w}]))"
        p95 = self._by_caller(f"1000 * histogram_quantile(0.95, {buckets})", window).get("", {})
        tokens: dict[str, list[float]] = {}
        span = f"{max(1, int((end - start).total_seconds()))}s"
        for direction, index in (("prompt", 0), ("completion", 1)):
            query = (
                "sum by (caller) (increase(mlp_gateway_tokens_total"
                f'{{{sel},direction="{direction}"}}[{span}]))'
            )
            for who, count in self._instant(query, end).items():
                tokens.setdefault(who, [0.0, 0.0])[index] = count
        return EndpointUsageSeries(
            callers=callers,
            tokens={c: (t[0], t[1]) for c, t in tokens.items() if c != "(anonymous)"},
            p95_latency_ms=[
                Sample(datetime.fromtimestamp(t, UTC), v) for t, v in sorted(p95.items())
            ],
        )

    def _instant(self, query: str, at: datetime) -> dict[str, float]:
        params = urllib.parse.urlencode({"query": query, "time": at.timestamp()})
        body = _get(f"{self._base}/api/v1/query?{params}")
        out: dict[str, float] = {}
        for result in body.get("data", {}).get("result", []):
            value = float(result["value"][1])
            if value == value:
                out[result.get("metric", {}).get("caller", "")] = value
        return out

    def _by_caller(self, query: str, window: dict[str, Any]) -> dict[str, dict[float, float]]:
        params = urllib.parse.urlencode({"query": query, **window})
        body = _get(f"{self._base}/api/v1/query_range?{params}")
        out: dict[str, dict[float, float]] = {}
        for result in body.get("data", {}).get("result", []):
            caller = result.get("metric", {}).get("caller", "")
            values = {
                float(ts): float(raw) for ts, raw in result["values"] if float(raw) == float(raw)
            }
            if values:
                out.setdefault(caller, {}).update(values)
        return out
