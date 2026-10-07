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
from typing import Any

import pytest

from controlplane.adapters.metrics import (
    PrometheusMetricsProvider,
    PrometheusPlatformTelemetry,
    PrometheusUsage,
)
from controlplane.application.platform import Health, PlatformService
from controlplane.application.providers import PlatformSignal
from controlplane.persistence.memory import MemoryStore, MemoryUnitOfWork

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
    lines += _platform_lines(start, end)
    lines.append("# EOF")
    return "\n".join(lines) + "\n"


STALL = 1200  # the rollouts reconciler stops this many seconds before the end


def _platform_lines(start: int, end: int) -> list[str]:
    """The control plane's own metrics: the runs reconciler passes 6 times a minute, the
    rollouts one stopped 20 minutes ago; 5% of serving calls fail; 2% of API calls are 5xx."""
    rec = 'job="mlp-controlplane-reconciler"'
    api = 'job="mlp-controlplane-api"'
    out = ["# TYPE mlp_reconcile_passes counter"]
    for name in ("runs", "rollouts"):
        for t in range(start, end + 1, STEP):
            alive = min(t, end - STALL) if name == "rollouts" else t
            passes = (alive - start) / 10
            out.append(f'mlp_reconcile_passes_total{{{rec},reconciler="{name}"}} {passes} {t}')
    out.append("# TYPE mlp_provider_calls counter")
    for outcome, share in (("ok", 0.95), ("error", 0.05)):
        for t in range(start, end + 1, STEP):
            n = (t - start) * 2 * share
            out.append(
                f'mlp_provider_calls_total{{provider="serving",outcome="{outcome}"}} {n:.1f} {t}'
            )
    out.append("# TYPE http_server_request_duration_seconds histogram")
    for code, share in (("200", 0.98), ("500", 0.02)):
        sel = f'{api},http_response_status_code="{code}"'
        for t in range(start, end + 1, STEP):
            n = (t - start) * 10 * share
            for le, under in (("0.1", 0.9), ("0.5", 1.0), ("+Inf", 1.0)):
                out.append(
                    f'http_server_request_duration_seconds_bucket{{{sel},le="{le}"}} '
                    f"{n * under:.1f} {t}"
                )
            out.append(f"http_server_request_duration_seconds_count{{{sel}}} {n:.1f} {t}")
            out.append(f"http_server_request_duration_seconds_sum{{{sel}}} {n * 0.05:.2f} {t}")
    return out + _gateway_lines(start, end)


def _gateway_lines(start: int, end: int) -> list[str]:
    """Gateway traffic to credit-risk-prod: partner-acme 120 calls/min with 1 in 60 refused,
    batch-job 30 calls/min with 1 in 30 failing upstream. Calls take ~0.2s."""
    where = 'project="credit-risk",endpoint="credit-risk-prod"'
    flows = (  # caller, code, calls per minute
        ("partner-acme", "200", 118),
        ("partner-acme", "429", 2),
        ("batch-job", "200", 29),
        ("batch-job", "502", 1),
    )
    out = ["# TYPE mlp_gateway_requests counter"]
    for caller, code, per_min in flows:
        for t in range(start, end + 1, STEP):
            n = (t - start) * per_min / 60
            sel = f'{where},caller="{caller}",code="{code}"'
            out.append(f"mlp_gateway_requests_total{{{sel}}} {n:.2f} {t}")
            if int(code) >= 400:
                out.append(f"mlp_gateway_usage_requests_total{{{sel}}} {n:.2f} {t}")
    out.append("# TYPE mlp_gateway_units counter")
    for caller, per_min in (("partner-acme", 118), ("batch-job", 29)):
        for t in range(start, end + 1, STEP):
            n = (t - start) * per_min / 60
            sel = f'{where},caller="{caller}",unit="requests"'
            out.append(f"mlp_gateway_units_total{{{sel}}} {n:.2f} {t}")
    out.append("# TYPE mlp_gateway_duration_seconds histogram")
    for t in range(start, end + 1, STEP):
        n = (t - start) * 150 / 60
        for le, under in (("0.1", 0.1), ("0.25", 0.97), ("1", 1.0), ("+Inf", 1.0)):
            out.append(
                f'mlp_gateway_duration_seconds_bucket{{{where},le="{le}"}} {n * under:.2f} {t}'
            )
        out.append(f"mlp_gateway_duration_seconds_count{{{where}}} {n:.2f} {t}")
        out.append(f"mlp_gateway_duration_seconds_sum{{{where}}} {n * 0.2:.2f} {t}")
    # an LLM endpoint: helpdesk sends 600 prompt and gets 300 completion tokens a minute
    llm = 'project="support",endpoint="assistant-prod",caller="helpdesk"'
    out.append("# TYPE mlp_gateway_tokens counter")
    for direction, per_min in (("prompt", 600), ("completion", 300)):
        for t in range(start, end + 1, STEP):
            n = (t - start) * per_min / 60
            out.append(f'mlp_gateway_tokens_total{{{llm},direction="{direction}"}} {n:.1f} {t}')
    return out


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


def test_platform_signals_from_real_prometheus(prometheus: tuple[str, int]) -> None:
    url, end = prometheus
    telemetry = PrometheusPlatformTelemetry(url, window="5m")
    at = datetime.fromtimestamp(end, UTC)
    start = at - timedelta(minutes=30)

    def series(signal: PlatformSignal) -> Any:
        return telemetry.platform_series(signal, start=start, end=at, step_seconds=60)

    passes = series(PlatformSignal.RECONCILE_PASSES)
    assert set(passes) == {"runs", "rollouts"}
    assert passes["runs"][-1].value == pytest.approx(6, rel=0.05)
    assert passes["rollouts"][-1].value == pytest.approx(0, abs=0.01)  # stopped

    errors = series(PlatformSignal.PROVIDER_ERRORS)
    assert errors["serving"][-1].value == pytest.approx(0.05, rel=0.05)
    api = series(PlatformSignal.API_ERRORS)
    assert api[""][-1].value == pytest.approx(0.02, rel=0.05)
    rps = series(PlatformSignal.API_REQUESTS)
    assert rps[""][-1].value == pytest.approx(10, rel=0.05)
    p95 = series(PlatformSignal.API_LATENCY)
    assert 50 < p95[""][-1].value < 500  # milliseconds, from second buckets

    store = MemoryStore()
    health = PlatformService(lambda: MemoryUnitOfWork(store), telemetry, clock=lambda: at).health(
        minutes=30
    )
    by_key = {s.spec.signal: s for s in health.signals}
    heartbeat = {s.name: s.status for s in by_key[PlatformSignal.RECONCILE_PASSES].series}
    assert heartbeat == {"rollouts": Health.CRITICAL, "runs": Health.OK}
    assert by_key[PlatformSignal.PROVIDER_ERRORS].status is Health.WARNING
    assert by_key[PlatformSignal.API_ERRORS].status is Health.WARNING
    assert health.status is Health.CRITICAL and health.available


def test_gateway_usage_by_caller_from_real_prometheus(prometheus: tuple[str, int]) -> None:
    url, end = prometheus
    at = datetime.fromtimestamp(end, UTC)
    usage = PrometheusUsage(url).endpoint_usage(
        "credit-risk", "credit-risk-prod", start=at - timedelta(minutes=30), end=at, step_seconds=60
    )
    acme, batch = usage.callers["partner-acme"][-1], usage.callers["batch-job"][-1]
    assert acme.units == pytest.approx(118, rel=0.05) and acme.rejected == pytest.approx(2, rel=0.1)
    assert acme.errors == 0
    assert batch.units == pytest.approx(29, rel=0.05) and batch.errors == pytest.approx(1, rel=0.1)
    assert 100 < usage.p95_latency_ms[-1].value < 250  # ms, from second buckets
    with pytest.raises(ValueError):
        PrometheusUsage(url).endpoint_usage('x"}', "y", start=at, end=at, step_seconds=60)

    telemetry = PrometheusPlatformTelemetry(url)
    window = {"start": at - timedelta(minutes=30), "end": at, "step_seconds": 60}
    rate = telemetry.platform_series(PlatformSignal.GATEWAY_REQUESTS, **window)  # type: ignore[arg-type]
    assert rate[""][-1].value == pytest.approx(150 / 60, rel=0.05)
    errors = telemetry.platform_series(PlatformSignal.GATEWAY_ERRORS, **window)  # type: ignore[arg-type]
    assert errors[""][-1].value == pytest.approx(1 / 150, rel=0.1)


def test_llm_tokens_by_caller_and_direction(prometheus: tuple[str, int]) -> None:
    url, end = prometheus
    at = datetime.fromtimestamp(end, UTC)
    usage = PrometheusUsage(url).endpoint_usage(
        "support", "assistant-prod", start=at - timedelta(minutes=30), end=at, step_seconds=60
    )
    prompt, completion = usage.tokens["helpdesk"]
    assert prompt == pytest.approx(600 * 30, rel=0.05)
    assert completion == pytest.approx(300 * 30, rel=0.05)
    rate = PrometheusPlatformTelemetry(url).platform_series(
        PlatformSignal.GATEWAY_TOKENS, start=at - timedelta(minutes=10), end=at, step_seconds=60
    )
    assert rate["prompt"][-1].value == pytest.approx(600, rel=0.05)
