#!/usr/bin/env python3
"""429-only worker/pool experiment driver inside a disposable acceptance job.

The unique caller bucket is deliberately indebted; no endpoint budget is modified.
Credentials come only from secret-backed environment variables and are never printed.
"""

import argparse
import json
import math
import os
import secrets
import subprocess
import threading
import time

import httpx
from controlplane_limiter_check import limiter_histogram_delta
from sqlalchemy import text

from controlplane.persistence.sql import make_engine


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", required=True)
    p.add_argument("--runtime-url", required=True)
    p.add_argument("--instance", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--endpoint", required=True)
    p.add_argument("--caller", required=True)
    p.add_argument("--workers", type=int, required=True, choices=[12, 24])
    p.add_argument("--pool", type=int, required=True, choices=[15, 30])
    p.add_argument("--otel-mode", choices=["on", "off"], default="on")
    p.add_argument("--rps", type=int, default=500)
    p.add_argument("--duration-seconds", type=int, default=10)
    p.add_argument("--connection-mode", choices=["baseline", "short-idle", "no-keepalive"])
    p.add_argument("--queue-size", type=int, default=5000)
    p.add_argument("--profile-cpu", action="store_true")
    p.add_argument("--contract-smoke", action="store_true")
    args = p.parse_args()
    if not args.project.startswith("acceptance-") or not args.caller.startswith("worker-probe-"):
        p.error("requires disposable acceptance project/caller")
    if args.rps <= 0 or args.queue_size <= 0:
        p.error("rps and queue size must be positive")
    if not 1 <= args.duration_seconds <= 600:
        p.error("duration must be 1–600 seconds")
    expected = args.rps * args.duration_seconds
    engine = make_engine(os.environ["CP_ACCEPTANCE_ADMIN_DATABASE_URL"])
    token = os.environ["CP_ACCEPTANCE_GATEWAY_TOKEN"]
    bucket = f"caller:{args.project}/{args.caller}/{args.endpoint}"
    selector = (
        '{job="mlp-gateway",instance="' + args.instance + '",'
        '__name__=~"mlp_gateway_phase_duration_seconds_(bucket|count|sum)|'
        "mlp_db_pool_acquire_duration_seconds_(bucket|count|sum)|"
        "mlp_db_query_duration_seconds_(bucket|count|sum)|"
        "mlp_gateway_event_loop_lag_seconds_(bucket|count|sum)|"
        "mlp_gateway_requests_total|mlp_gateway_usage_requests_total|mlp_db_errors_total|"
        'mlp_gateway_limiter_workers|mlp_gateway_inflight|mlp_db_pool_checked_out"}'
    )
    prom = "http://prometheus.observability:9090/api/v1"

    def snapshot(client):
        r = client.get(prom + "/query", params={"query": selector})
        r.raise_for_status()
        return r.json()["data"]["result"]

    stopped = threading.Event()
    locks = []
    monitor_errors = []
    runtime_samples = []

    def monitor():
        last_runtime = 0
        last_progress = 0
        with httpx.Client(timeout=5) as monitor_client:
            while not stopped.is_set():
                try:
                    with engine.connect() as conn:
                        row = conn.execute(
                            text("SELECT count(*) FILTER (WHERE NOT granted) FROM pg_locks")
                        ).scalar_one()
                    locks.append(int(row))
                    now = time.time()
                    if args.duration_seconds >= 60 and now - last_runtime >= 5:
                        sample = monitor_client.get(args.runtime_url).json()
                        q = (
                            'sum(mlp_gateway_requests_total{job="mlp-gateway",instance="'
                            + args.instance
                            + '"})'
                        )
                        response = monitor_client.get(prom + "/query", params={"query": q})
                        response.raise_for_status()
                        values = response.json()["data"]["result"]
                        sample["exported_request_counter"] = (
                            float(values[0]["value"][1]) if values else None
                        )
                        runtime_samples.append(sample)
                        last_runtime = now
                        if now - last_progress >= 30:
                            print(
                                json.dumps(
                                    {
                                        "progress": True,
                                        "elapsed_seconds": now - start,
                                        "gateway_cpu_cores": (
                                            sample["cpu"]["usage_usec"]
                                            - cpu_before["cpu"]["usage_usec"]
                                        )
                                        / 1e6
                                        / max(0.001, now - start),
                                        "memory_mib": sample["memory_bytes"] / 1024**2,
                                        "python_threads": sample.get("active_python_threads"),
                                        "goroutines": sample.get("goroutines"),
                                        "exported_request_counter": sample[
                                            "exported_request_counter"
                                        ],
                                    }
                                ),
                                flush=True,
                            )
                            last_progress = now
                except Exception as exc:
                    monitor_errors.append(type(exc).__name__)
                stopped.wait(0.2)

    contract_checks = []
    upstream_smoke_trace_id = None
    try:
        if args.contract_smoke:
            upstream_smoke_trace_id = secrets.token_hex(16)
            with httpx.Client(timeout=40) as smoke:
                base_headers = {"authorization": "Bearer " + token, "x-request-id": "poc-contract"}
                response = smoke.post(
                    args.url,
                    json={},
                    headers={
                        **base_headers,
                        "traceparent": f"00-{upstream_smoke_trace_id}-{secrets.token_hex(8)}-01",
                    },
                )
                contract_checks.append(
                    {
                        "case": "real-function-forward",
                        "status": response.status_code,
                        "passed": response.status_code == 200,
                        "has_rate_limit_headers": all(
                            k in response.headers
                            for k in ["ratelimit-limit", "ratelimit-remaining", "ratelimit-reset"]
                        ),
                        "request_id_preserved": response.headers.get("x-request-id")
                        == "poc-contract",
                    }
                )
                if not all(
                    [
                        contract_checks[-1]["passed"],
                        contract_checks[-1]["has_rate_limit_headers"],
                        contract_checks[-1]["request_id_preserved"],
                    ]
                ):
                    raise RuntimeError("real function forwarding contract failed")
                wrong = token[:-1] + ("a" if token[-1] != "a" else "b")
                cases = [
                    ("missing-token", args.url, {}, 401, "unauthenticated"),
                    (
                        "wrong-secret",
                        args.url,
                        {"authorization": "Bearer " + wrong},
                        401,
                        "unauthenticated",
                    ),
                    (
                        "wrong-operation",
                        args.url.rsplit("/", 1)[0] + "/predict",
                        base_headers,
                        404,
                        "not_found",
                    ),
                    (
                        "unknown-endpoint",
                        args.url.replace("/" + args.endpoint + "/", "/poc-missing/"),
                        base_headers,
                        404,
                        "not_found",
                    ),
                ]
                for name, url, headers, status, code in cases:
                    response = smoke.post(url, json={}, headers=headers)
                    passed = (
                        response.status_code == status
                        and response.json().get("error", {}).get("code") == code
                    )
                    contract_checks.append(
                        {"case": name, "status": response.status_code, "passed": passed}
                    )
                    if not passed:
                        raise RuntimeError("HTTP refusal contract failed")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO gateway_rate_buckets(name,tokens,updated_at) "
                    "VALUES (:name,:debt,extract(epoch from clock_timestamp())) "
                    "ON CONFLICT(name) DO UPDATE SET tokens=:debt, "
                    "updated_at=extract(epoch from clock_timestamp())"
                ),
                {"name": bucket, "debt": -max(6000, (args.duration_seconds + 120) * 10)},
            )
        trace_id = secrets.token_hex(16) if args.otel_mode == "on" else None
        with httpx.Client(timeout=10) as c:
            for warmup_index in range(100):
                headers = {"authorization": "Bearer " + token}
                if warmup_index == 0 and trace_id:
                    headers["traceparent"] = f"00-{trace_id}-{secrets.token_hex(8)}-01"
                r = c.post(args.url, json={}, headers=headers)
                if r.status_code != 429:
                    raise RuntimeError("fixture warmup was not 429-only")
            c.post(args.runtime_url + "/telemetry/flush").raise_for_status()
            time.sleep(5)
            before = snapshot(c)
            cpu_before = c.get(args.runtime_url).json()
            if cpu_before["otel_sdk_disabled"] != (args.otel_mode == "off"):
                raise RuntimeError("probe SDK mode does not match requested case")
            if args.profile_cpu:
                c.post(args.runtime_url + "/profile/start").raise_for_status()
            start = time.time()
            observer = threading.Thread(target=monitor)
            observer.start()
            try:
                connection_args = []
                if args.connection_mode:
                    connection_args = ["--connection-diagnostics"]
                    if args.connection_mode == "short-idle":
                        connection_args += ["--idle-conn-timeout", "2s"]
                    elif args.connection_mode == "no-keepalive":
                        connection_args += ["--disable-keep-alives"]
                process = subprocess.run(
                    [
                        "mlp-loadgen",
                        "--rps",
                        str(args.rps),
                        "--duration",
                        f"{args.duration_seconds}s",
                        "--concurrency",
                        "200",
                        "--queue-size",
                        str(args.queue_size),
                        "--timeout",
                        "15s",
                        "--targets",
                        args.url,
                        "--payload",
                        "{}",
                    ]
                    + connection_args,
                    env={**os.environ, "MLP_LOADGEN_TOKEN": token},
                    capture_output=True,
                    text=True,
                    timeout=args.duration_seconds + 45,
                )
                finish = time.time()
                profile = None
                if args.profile_cpu:
                    profile_response = c.post(args.runtime_url + "/profile/stop", timeout=30)
                    profile_response.raise_for_status()
                    profile = profile_response.json()
                cpu_after = c.get(args.runtime_url).json()
            finally:
                stopped.set()
                observer.join(timeout=10)
            result = json.loads(process.stdout)
            if args.connection_mode:
                expected_idle = 2 if args.connection_mode == "short-idle" else 90
                if (
                    not result.get("connection_diagnostics")
                    or result.get("keep_alive_disabled") != (args.connection_mode == "no-keepalive")
                    or result.get("idle_conn_timeout_seconds") != expected_idle
                    or (
                        args.connection_mode == "short-idle"
                        and expected_idle >= cpu_before["server_keep_alive_seconds"]
                    )
                ):
                    raise RuntimeError("transport settings do not match requested experiment")
            c.post(args.runtime_url + "/telemetry/flush").raise_for_status()
            time.sleep(5)
            after = snapshot(c)
            phases = {}

            def group(series):
                labels = series["metric"]
                return labels.get("phase") or labels["__name__"].rsplit("_", 1)[0]

            histogram = [
                s for s in after if s["metric"]["__name__"].endswith(("_bucket", "_count", "_sum"))
            ]
            for key in sorted({group(s) for s in histogram}):
                try:
                    phases[key] = limiter_histogram_delta(
                        [s for s in before if group(s) == key],
                        [s for s in histogram if group(s) == key],
                    )
                except ValueError:
                    phases[key] = {"missing_or_incomplete": True}
            gauge_query = (
                '{job="mlp-gateway",instance="' + args.instance + '",'
                '__name__=~"mlp_gateway_limiter_workers|mlp_gateway_inflight|mlp_db_pool_checked_out"}'
            )
            r = c.get(
                prom + "/query_range",
                params={"query": gauge_query, "start": start, "end": finish + 2, "step": 1},
            )
            r.raise_for_status()
            gauges = {
                s["metric"]["__name__"]: max(float(v) for _, v in s["values"])
                for s in r.json()["data"]["result"]
                if s["values"]
            }
            result.update(
                contract_checks=contract_checks,
                upstream_smoke_trace_id=upstream_smoke_trace_id,
                duration_seconds=args.duration_seconds,
                load_started_at_epoch=start,
                load_finished_at_epoch=finish,
                runtime_samples=runtime_samples,
                gateway_memory_before_bytes=cpu_before["memory_bytes"],
                server_keep_alive_seconds=cpu_before["server_keep_alive_seconds"],
                gateway_memory_peak_bytes=cpu_after["memory_peak_bytes"],
                gateway_threads_before=cpu_before.get("active_python_threads"),
                gateway_threads_after=cpu_after.get("active_python_threads"),
                gateway_goroutines_before=cpu_before.get("goroutines"),
                gateway_goroutines_after=cpu_after.get("goroutines"),
                otel_mode=args.otel_mode,
                warmup_trace_id=trace_id,
                telemetry_profile=cpu_before["telemetry_profile"],
                queue_capacity=args.queue_size,
                cpu_profile=profile,
                workers=args.workers,
                pool_capacity=args.pool,
                exit_code=process.returncode,
                phases=phases,
                max_observed_gauges=gauges,
                gateway_cpu_stat_delta={
                    k: cpu_after["cpu"][k] - v for k, v in cpu_before["cpu"].items()
                },
                gateway_cpu_window_seconds=finish - start,
                gateway_memory_bytes=cpu_after["memory_bytes"],
                cluster_lock_wait_samples=locks,
                monitor_errors=monitor_errors,
                scope=(
                    "429-only; shared budget preserved; pg_locks waiters cluster-wide, "
                    "not exact row-lock duration"
                ),
            )
            result["availability_passed"] = result["status_codes"] == {"429": expected}
            result["pool_acquire_errors"] = sum(
                float(s["value"][1])
                for s in after
                if s["metric"]["__name__"] == "mlp_db_pool_acquire_duration_seconds_count"
                and s["metric"].get("outcome") == "error"
            ) - sum(
                float(s["value"][1])
                for s in before
                if s["metric"]["__name__"] == "mlp_db_pool_acquire_duration_seconds_count"
                and s["metric"].get("outcome") == "error"
            )

            def counter_delta(metric):
                return sum(
                    float(s["value"][1]) for s in after if s["metric"]["__name__"] == metric
                ) - sum(float(s["value"][1]) for s in before if s["metric"]["__name__"] == metric)

            rate = cpu_before["telemetry_profile"]["latency_sample_rate"]
            tolerance = max(20, 6 * math.sqrt(expected * rate * (1 - rate)))
            result["exact_request_metric_delta"] = counter_delta("mlp_gateway_requests_total")
            result["db_error_metric_delta"] = counter_delta("mlp_db_errors_total")
            result["request_accounting_passed"] = (
                result["planned"] == result["offered"] + result["unscheduled"]
                and result["offered"]
                == result["started"] + result["queue_dropped"] + result["canceled_before_start"]
                and result["started"] == result["completed"] + result["transport_errors"]
            )
            result["capacity_passed"] = result["completed_within_window_rps"] >= args.rps * 0.99
            result["integrity_passed"] = (
                process.returncode == 0
                and result["completed"] == expected
                and sum(result["status_codes"].values()) == expected
                and set(result["status_codes"]) <= {"429", "503"}
                and (
                    result["exact_request_metric_delta"] == expected
                    and all(
                        abs(phases.get(key, {}).get("observations", 0) - expected * rate)
                        <= tolerance
                        for key in ["limiter.queue", "limiter.work", "limiter.total"]
                    )
                    if args.otel_mode == "on"
                    else not after
                )
                and sum(
                    float(s["value"][1])
                    for s in after
                    if s["metric"]["__name__"] == "mlp_gateway_phase_duration_seconds_count"
                    and s["metric"].get("phase", "").startswith("upstream")
                )
                == sum(
                    float(s["value"][1])
                    for s in before
                    if s["metric"]["__name__"] == "mlp_gateway_phase_duration_seconds_count"
                    and s["metric"].get("phase", "").startswith("upstream")
                )
            )
            print(json.dumps(result), flush=True)
            # Availability failures are experiment results, not lost request accounting.
            if not result["integrity_passed"] and args.duration_seconds < 60:
                raise RuntimeError("429-only request integrity failed; result retained")
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM gateway_rate_buckets WHERE name=:name"), {"name": bucket}
            )
        engine.dispose()


if __name__ == "__main__":
    main()
