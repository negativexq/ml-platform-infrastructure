"""Moving counters in a canary snapshot must use the same Prometheus instant."""

import io
import json
from urllib.parse import parse_qs, urlparse

import pytest

from controlplane.adapters.metrics import prometheus


def test_revision_snapshot_queries_share_time_and_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    values = iter([100, 5, 2, 12])

    def open_query(url: str, **kwargs: object) -> io.BytesIO:
        calls.append(parse_qs(urlparse(url).query))
        return io.BytesIO(
            json.dumps({"data": {"result": [{"value": [0, str(next(values))]}]}}).encode()
        )

    monkeypatch.setattr(prometheus.urllib.request, "urlopen", open_query)
    result = prometheus.PrometheusMetricsProvider("http://prometheus").revision_metrics(
        "mlp-project/model", 2, "model-predictor-00002"
    )
    assert result.error_rate == 0.05
    assert result.requests == 100 and result.p95_latency_ms == 12
    assert len(calls) == 4 and len({call["time"][0] for call in calls}) == 1
    assert all('revision_name="model-predictor-00002"' in call["query"][0] for call in calls)
    assert all('namespace_name="mlp-project"' in call["query"][0] for call in calls)
