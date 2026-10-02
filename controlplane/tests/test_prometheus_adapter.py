"""The Prometheus adapter against a real Prometheus, with data shaped like Knative's
per-revision metrics written straight into its TSDB (promtool). Skipped without the binaries
(set PROMETHEUS_HOME to an extracted prometheus-*.linux-amd64 directory)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from controlplane.adapters.metrics import PrometheusMetricsProvider

HOME = Path(os.environ.get("PROMETHEUS_HOME", "/tmp/claude-0/bin/prometheus-3.1.0.linux-amd64"))
NS, CANARY, STABLE = (
    "mlp-credit-risk",
    "credit-risk-prod-predictor-00002",
    "credit-risk-prod-predictor-00001",
)
BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, float("inf"))  # seconds
SPAN, STEP = 3600, 15  # an hour of samples, every 15s


def _openmetrics(end: int) -> str:
    """Counters that grow at a known rate: stable 20 rps with 1% 5xx, canary 5 rps, 10% 5xx.
    Latencies: stable mostly < 100ms, canary mostly < 250ms."""
    # Knative exports these without the `_total` suffix, so they are written as gauges here.
    lines = [
        "# TYPE revision_request_count gauge",
        "# TYPE revision_request_latencies_bucket gauge",
        "# TYPE revision_request_latencies_count gauge",
        "# TYPE revision_request_latencies_sum gauge",
    ]
    start = end - SPAN
    for revision, rps, bad, under in (
        (STABLE, 20, 0.01, (0.4, 0.95, 1, 1, 1, 1)),
        (CANARY, 5, 0.10, (0.1, 0.5, 0.97, 1, 1, 1)),
    ):
        sel = f'namespace_name="{NS}",revision_name="{revision}"'
        for t in range(start, end + 1, STEP):
            n = (t - start) * rps
            lines.append(
                f'revision_request_count{{{sel},response_code_class="2xx"}} {n * (1 - bad):.1f} {t}'
            )
            lines.append(
                f'revision_request_count{{{sel},response_code_class="5xx"}} {n * bad:.1f} {t}'
            )
            for le, share in zip(BUCKETS, under, strict=True):
                bound = "+Inf" if le == float("inf") else f"{le * 1000:g}"
                lines.append(
                    f'revision_request_latencies_bucket{{{sel},le="{bound}"}} {n * share:.1f} {t}'
                )
            lines.append(f"revision_request_latencies_count{{{sel}}} {n:.1f} {t}")
            lines.append(f"revision_request_latencies_sum{{{sel}}} {n * 80:.1f} {t}")
    lines.append("# EOF")
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="module")
def prometheus() -> Iterator[tuple[str, int]]:
    if not (HOME / "prometheus").exists():
        pytest.skip("no Prometheus binaries (set PROMETHEUS_HOME)")
    work = Path(tempfile.mkdtemp(prefix="cp-prom-"))
    end = int(time.time()) - 60
    (work / "data.om").write_text(_openmetrics(end))
    subprocess.run(
        [
            str(HOME / "promtool"),
            "tsdb",
            "create-blocks-from",
            "openmetrics",
            str(work / "data.om"),
            str(work / "tsdb"),
        ],
        check=True,
        capture_output=True,
    )
    (work / "prom.yml").write_text("global: {scrape_interval: 1h}\n")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [
            str(HOME / "prometheus"),
            f"--config.file={work / 'prom.yml'}",
            f"--storage.tsdb.path={work / 'tsdb'}",
            f"--web.listen-address=127.0.0.1:{port}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if urllib.request.urlopen(f"{url}/-/ready", timeout=1).status == 200:  # noqa: S310
                break
        except OSError:
            time.sleep(0.2)
    yield url, end
    proc.terminate()
    proc.wait(timeout=10)
    shutil.rmtree(work, ignore_errors=True)


def test_history_per_revision(prometheus: tuple[str, int]) -> None:
    url, end = prometheus
    provider = PrometheusMetricsProvider(url, window="2m")
    stop = datetime.fromtimestamp(end, UTC)
    canary = provider.revision_history(
        f"{NS}/credit-risk-prod",
        2,
        CANARY,
        start=stop - timedelta(minutes=30),
        end=stop,
        step_seconds=60,
    )
    stable = provider.revision_history(
        f"{NS}/credit-risk-prod",
        1,
        STABLE,
        start=stop - timedelta(minutes=30),
        end=stop,
        step_seconds=60,
    )
    assert 28 <= len(canary) <= 31 and 28 <= len(stable) <= 31
    last_c, last_s = canary[-1], stable[-1]
    assert last_c.requests_per_second == pytest.approx(5, rel=0.05)
    assert last_s.requests_per_second == pytest.approx(20, rel=0.05)
    assert last_c.error_rate == pytest.approx(0.10, rel=0.05)
    assert last_s.error_rate == pytest.approx(0.01, rel=0.05)
    assert last_c.p95_latency_ms is not None and last_s.p95_latency_ms is not None
    assert 100 < last_c.p95_latency_ms <= 250 and 50 < last_s.p95_latency_ms <= 100
    assert [p.at for p in canary] == sorted(p.at for p in canary)


def test_no_backend_revision_means_no_history(prometheus: tuple[str, int]) -> None:
    url, end = prometheus
    stop = datetime.fromtimestamp(end, UTC)
    provider = PrometheusMetricsProvider(url)
    assert (
        provider.revision_history(
            "ns/x", 1, None, start=stop - timedelta(minutes=5), end=stop, step_seconds=60
        )
        == []
    )
    unknown = provider.revision_history(
        f"{NS}/x", 3, "nope-00009", start=stop - timedelta(minutes=5), end=stop, step_seconds=60
    )
    assert unknown == []
