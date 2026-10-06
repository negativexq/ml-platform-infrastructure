#!/usr/bin/env python3
"""Run inside an isolated cluster fixture, using four distinct gateway pod URLs.

Requires existing project/endpoint/key and explicitly supplied database credentials.
Resets only the named acceptance endpoint/caller buckets between completed samples.
Never print bearer tokens or DB URLs. Mount this and controlplane_limiter_check.py in
one ConfigMap when using the control-plane image as a Kubernetes Job generator.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import uvloop
from sqlalchemy import text

from controlplane.persistence.sql import make_engine

try:
    from .controlplane_limiter_check import limiter_histogram_delta, limiter_snapshot, sample
except ImportError:  # direct script/ConfigMap execution
    from controlplane_limiter_check import limiter_histogram_delta, limiter_snapshot, sample


def sample_passed(result: dict[str, Any]) -> bool:
    # Queue/scheduling guards prevent a slow generator or draining backlog from
    # being reported as sustainable offered load. These are fixture guards, not
    # a production inference latency SLO.
    return (
        result["shared_budget_upper_bound_holds"]
        and result["allowed"] > 0
        and result["limiter_observation_count_matches"]
        and not any(
            count
            for code, count in result["statuses"].items()
            if code in {"transport_error", "db_sample_error"} or code.isdigit() and int(code) >= 500
        )
        and result["completed_rps"] >= 0.9 * result["offered_rps"]
        and result["client_queue_latency_ms"]["p95"] <= 100
        and result["scheduling_lag_p95_ms"] <= 100
    )


async def run(args: argparse.Namespace) -> dict:
    token = os.environ["CP_ACCEPTANCE_GATEWAY_TOKEN"]
    engine = make_engine(os.environ["CP_ACCEPTANCE_ADMIN_DATABASE_URL"])
    names = [
        f"endpoint:{args.project}/{args.endpoint}",
        f"caller:{args.project}/{args.caller}/{args.endpoint}",
    ]
    report = {
        "passed": False,
        "scope": "In-cluster generator; distinct gateway pod IPs. Isolated cumulative OTLP "
        "histogram deltas; histogram quantiles are interpolated estimates. "
        "Low-budget traffic tests admission/rejection, not 500 successful inference RPS.",
        "target_replicas": args.replicas,
        "seconds_per_sample": args.seconds,
        "limit_units_per_minute": args.limit,
        "results": [],
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            for url in args.urls:
                response = await client.post(
                    url, json={}, headers={"authorization": "Bearer " + token}
                )
                response.raise_for_status()
        await asyncio.sleep(args.metric_flush_seconds)
        for replicas in args.replicas:
            for rate in args.rates:
                with engine.begin() as connection:
                    connection.execute(
                        text("DELETE FROM gateway_rate_buckets WHERE name IN (:endpoint,:caller)"),
                        {"endpoint": names[0], "caller": names[1]},
                    )
                before = await limiter_snapshot(args.prometheus)
                result = await sample(
                    args.urls[:replicas],
                    token,
                    rate,
                    args.seconds,
                    args.limit,
                    {},
                    os.environ["CP_ACCEPTANCE_DATABASE_URL"],
                )
                await asyncio.sleep(args.metric_flush_seconds)
                result["isolated_limiter_histogram_delta_ms"] = limiter_histogram_delta(
                    before, await limiter_snapshot(args.prometheus)
                )
                result["limiter_observation_count_matches"] = (
                    result["isolated_limiter_histogram_delta_ms"]["observations"]
                    == rate * args.seconds
                )
                report["results"].append(result)
                print(json.dumps(result), flush=True)
        report["passed"] = all(sample_passed(r) for r in report["results"])
    finally:
        engine.dispose()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--caller", required=True)
    parser.add_argument("--urls", nargs=4, required=True)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--limit", type=int, default=600)
    parser.add_argument("--rates", nargs="+", type=int, default=[50, 100, 500])
    parser.add_argument("--replicas", nargs="+", type=int, choices=[1, 2, 4], default=[1, 2, 4])
    parser.add_argument("--prometheus", required=True)
    parser.add_argument("--metric-flush-seconds", type=float, default=3)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--confirm-test-endpoint", action="store_true", required=True)
    args = parser.parse_args()
    if (
        not args.project.startswith("acceptance-")
        or len(set(args.urls)) != 4
        or not 1 <= args.seconds <= 60
        or args.limit <= 0
        or any(rate <= 0 for rate in args.rates)
        or args.metric_flush_seconds < 1
    ):
        parser.error(
            "isolated acceptance project, distinct replica URLs and bounded positive load required"
        )
    for url in args.urls:
        if urlsplit(url).path != f"/v1/{args.project}/{args.endpoint}/invoke":
            parser.error("replica URLs must match the dedicated HTTP-function fixture")
    for name in (
        "CP_ACCEPTANCE_GATEWAY_TOKEN",
        "CP_ACCEPTANCE_ADMIN_DATABASE_URL",
        "CP_ACCEPTANCE_DATABASE_URL",
    ):
        if not os.environ.get(name):
            parser.error(name + " is required")
    with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
        report = runner.run(run(args))
    report["generator_event_loop"] = "uvloop"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    print("REPORT_JSON=" + json.dumps(report), flush=True)
    if not report["passed"]:
        raise SystemExit("limiter load gate failed")


if __name__ == "__main__":
    main()
