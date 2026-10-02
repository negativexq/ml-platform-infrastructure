"""MetricsProvider backed by Prometheus, reading Knative/KServe per-revision metrics.

Judging a canary needs the canary's own numbers, so every query is scoped to one
backend revision (`revision_name`), never the whole service.

Not exercised against a real Prometheus/KServe yet: see docs/local-verification.md.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from controlplane.application.providers import MetricsPoint, RevisionMetrics

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
        requests = self._scalar(f"sum(increase(revision_request_count{{{sel}}}[{w}]))")
        errors = self._scalar(
            f'sum(increase(revision_request_count{{{sel},response_code_class="5xx"}}[{w}]))'
        )
        rps = self._scalar(f"sum(rate(revision_request_count{{{sel}}}[{w}]))")
        buckets = f"sum(rate(revision_request_latencies_bucket{{{sel}}}[{w}])) by (le)"
        p95 = self._scalar(f"histogram_quantile(0.95, {buckets})")
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
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
                body: dict[str, Any] = json.load(response)
        except urllib.error.URLError as exc:
            raise ConnectionError(f"prometheus query failed: {exc}") from exc
        return body

    def _scalar(self, query: str) -> float | None:
        url = f"{self._base}/api/v1/query?{urllib.parse.urlencode({'query': query})}"
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
