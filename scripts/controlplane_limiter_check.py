#!/usr/bin/env python3
"""Measure real gateway replicas. Uses explicit URLs/token and a dedicated test endpoint.

Run against 4 distinct gateway pod addresses (or port forwards), never the same load
balancer 4 times. No servers or databases are started by this command.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text

from controlplane.persistence.sql import make_engine


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)] if ordered else 0


async def sample(
    urls: list[str],
    token: str,
    rps: int,
    seconds: int,
    limit: int,
    body: dict[str, Any],
    database_url: str | None,
) -> dict[str, Any]:
    latencies: list[float] = []
    codes: Counter[str] = Counter()
    locks: list[int] = []
    semaphore = asyncio.Semaphore(200)
    engine = make_engine(database_url) if database_url else None
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=15, limits=httpx.Limits(max_connections=200)) as client:

        async def request(index: int) -> None:
            target = started + index / rps
            await asyncio.sleep(max(0, target - time.monotonic()))
            before = time.monotonic()
            async with semaphore:
                try:
                    response = await client.post(
                        urls[index % len(urls)],
                        json=body,
                        headers={"authorization": "Bearer " + token},
                    )
                    codes[str(response.status_code)] += 1
                except httpx.HTTPError:
                    codes["transport_error"] += 1
            latencies.append((time.monotonic() - before) * 1000)

        async def observe() -> None:
            if engine is None:
                return

            def read() -> int:
                with engine.connect() as conn:
                    conn.execute(text("SET LOCAL statement_timeout='1s'"))
                    return int(
                        conn.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE datname=current_database() "
                                "AND wait_event_type='Lock' AND query LIKE '%gateway_rate_buckets%'"
                            )
                        )
                        or 0
                    )

            while time.monotonic() - started < seconds:
                try:
                    locks.append(await asyncio.to_thread(read))
                except Exception:  # noqa: BLE001 - report missing samples, do not hide outages
                    codes["db_sample_error"] += 1
                await asyncio.sleep(0.25)

        try:
            await asyncio.gather(observe(), *(request(i) for i in range(rps * seconds)))
        finally:
            if engine:
                engine.dispose()
    elapsed = time.monotonic() - started
    limiter_latency: dict[str, Any] = {}
    prometheus = os.environ.get("CP_ACCEPTANCE_PROMETHEUS_URL")
    if prometheus:
        async with httpx.AsyncClient(timeout=5) as metrics_client:
            for name, quantile in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99)):
                query = (
                    f"histogram_quantile({quantile}, sum by(le)(rate("
                    'mlp_gateway_limiter_duration_seconds_bucket{operation="take"}[1m])))'
                )
                try:
                    response = await metrics_client.get(
                        prometheus.rstrip("/") + "/api/v1/query", params={"query": query}
                    )
                    response.raise_for_status()
                    series = response.json().get("data", {}).get("result", [])
                    value = float(series[0]["value"][1]) if series else float("nan")
                    limiter_latency[name] = value * 1000 if math.isfinite(value) else None
                except (httpx.HTTPError, ValueError, KeyError, IndexError):
                    limiter_latency[name] = None
    allowed = sum(n for code, n in codes.items() if code.isdigit() and int(code) < 400)
    maximum = math.ceil(limit + limit * elapsed / 60)
    return {
        "replicas": len(urls),
        "offered_rps": rps,
        "elapsed_seconds": elapsed,
        "statuses": dict(codes),
        "completed_rps": len(latencies) / elapsed,
        "limiter_latency_ms": limiter_latency or None,
        "limiter_metric_window_seconds": 60,
        "http_latency_ms": {
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
        },
        "max_observed_bucket_lock_waiters": max(locks, default=0) if locks else None,
        "allowed": allowed,
        "shared_budget_upper_bound": maximum,
        "shared_budget_upper_bound_holds": allowed <= maximum,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--urls", nargs=4, required=True)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--limit", type=int, required=True, help="endpoint AND caller units/minute")
    parser.add_argument("--body", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--confirm-test-endpoint", action="store_true", required=True)
    parser.add_argument("--scenario", choices=["load", "outage", "recovery"], default="load")
    args = parser.parse_args()
    if len(set(args.urls)) != 4 or not 1 <= args.seconds <= 60 or args.limit <= 0:
        parser.error("four distinct replica URLs, 1–60 seconds and a positive limit are required")
    token = os.environ.get("CP_ACCEPTANCE_GATEWAY_TOKEN")
    if not token:
        parser.error("CP_ACCEPTANCE_GATEWAY_TOKEN is required")
    body = json.loads(args.body.read_text())
    if not isinstance(body, dict):
        parser.error("request body must be a JSON object")
    results = []
    for replicas in (1, 2, 4):
        for rate in (50, 100, 500):
            result = asyncio.run(
                sample(
                    args.urls[:replicas],
                    token,
                    rate,
                    args.seconds,
                    args.limit,
                    body,
                    os.environ.get("CP_ACCEPTANCE_DATABASE_URL"),
                )
            )
            result["scenario"] = args.scenario
            results.append(result)
            print(json.dumps(result), flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=2) + "\n")
    if any(not r["shared_budget_upper_bound_holds"] for r in results):
        raise SystemExit("shared budget exceeded")
    if args.scenario == "outage" and any(r["allowed"] for r in results):
        raise SystemExit("outage forwarded successful requests")
    if args.scenario == "load" and any(
        r["statuses"].get("503", 0) or r["statuses"].get("transport_error", 0) for r in results
    ):
        raise SystemExit("load produced availability failures")


if __name__ == "__main__":
    main()
