#!/usr/bin/env python3
"""In-cluster paired generator comparison on an explicit disposable acceptance caller.

Uses CP_ACCEPTANCE_GATEWAY_TOKEN and CP_ACCEPTANCE_ADMIN_DATABASE_URL; never prints them.
Both generators get 200 connections, 15s request timeout and the same complete payload.
Go queue capacity equals the planned request count to match Python's bounded sample size.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from typing import Any

import httpx
import uvloop
from sqlalchemy import text

from controlplane.persistence.sql import make_engine

try:
    from .controlplane_limiter_check import limiter_histogram_delta, limiter_snapshot, sample
except ImportError:
    from controlplane_limiter_check import limiter_histogram_delta, limiter_snapshot, sample


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--caller", required=True)
    parser.add_argument("--urls", nargs=4, required=True)
    parser.add_argument("--prometheus", required=True)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--rps", type=int, default=500)
    parser.add_argument("--confirm-test-endpoint", action="store_true", required=True)
    args = parser.parse_args()
    if (
        not args.project.startswith("acceptance-")
        or len(set(args.urls)) != 4
        or not 1 <= args.seconds <= 60
        or not 1 <= args.rps <= 1000
    ):
        parser.error("Requires four distinct targets and bounded disposable acceptance load")
    token = os.environ["CP_ACCEPTANCE_GATEWAY_TOKEN"]
    engine = make_engine(os.environ["CP_ACCEPTANCE_ADMIN_DATABASE_URL"])
    names = [
        f"endpoint:{args.project}/{args.endpoint}",
        f"caller:{args.project}/{args.caller}/{args.endpoint}",
    ]
    results: list[dict[str, Any]] = []
    warm_trace_id = secrets.token_hex(16)
    try:
        with httpx.Client(timeout=120) as client:
            for url in args.urls:
                r = client.post(
                    url,
                    json={},
                    headers={
                        "authorization": "Bearer " + token,
                        "traceparent": f"00-{warm_trace_id}-0123456789abcdef-01",
                    },
                )
                r.raise_for_status()
        time.sleep(5)
        for replicas in [1, 2, 4]:
            # Alternate order to expose order sensitivity instead of always favoring one client.
            order = ["go", "python"] if replicas != 2 else ["python", "go"]
            for generator in order:
                with engine.begin() as conn:
                    conn.execute(
                        text("DELETE FROM gateway_rate_buckets WHERE name IN (:a,:b)"),
                        {"a": names[0], "b": names[1]},
                    )
                before = asyncio.run(limiter_snapshot(args.prometheus))
                urls = args.urls[:replicas]
                if generator == "go":
                    env = {**os.environ, "MLP_LOADGEN_TOKEN": token}
                    process = subprocess.run(
                        [
                            "mlp-loadgen",
                            "--rps",
                            str(args.rps),
                            "--duration",
                            f"{args.seconds}s",
                            "--concurrency",
                            "200",
                            "--queue-size",
                            str(args.rps * args.seconds),
                            "--timeout",
                            "15s",
                            "--targets",
                            ",".join(urls),
                            "--payload",
                            "{}",
                        ],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=args.seconds + 45,
                    )
                    result = json.loads(process.stdout)
                    result["exit_code"] = process.returncode
                else:
                    # Run Python in a separate process, like Go, so memory and startup don't
                    # accumulate across paired samples. Startup is outside its timed window.
                    process = subprocess.run(
                        [
                            sys.executable,
                            __file__,
                            "--python-child",
                            json.dumps({"urls": urls, "rps": args.rps, "seconds": args.seconds}),
                        ],
                        capture_output=True,
                        text=True,
                        timeout=args.seconds + 45,
                    )
                    if process.returncode:
                        raise RuntimeError("Python sample failed")
                    result = json.loads(process.stdout)
                time.sleep(5)
                result.update(
                    generator=generator,
                    replicas=replicas,
                    limiter=limiter_histogram_delta(
                        before, asyncio.run(limiter_snapshot(args.prometheus))
                    ),
                )
                results.append(result)
                print(json.dumps(result), flush=True)
    finally:
        engine.dispose()
    print(
        json.dumps(
            {
                "comparison": results,
                "warm_trace_id": warm_trace_id,
                "scope": "paired in-cluster low-budget limiter fixture; "
                "not admitted inference capacity",
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--python-child":
        import resource

        options = json.loads(sys.argv[2])
        start = resource.getrusage(resource.RUSAGE_SELF)
        with asyncio.Runner(loop_factory=uvloop.new_event_loop) as runner:
            result = runner.run(
                sample(
                    options["urls"],
                    os.environ["CP_ACCEPTANCE_GATEWAY_TOKEN"],
                    options["rps"],
                    options["seconds"],
                    600,
                    {},
                    None,
                )
            )
        end = resource.getrusage(resource.RUSAGE_SELF)
        result.update(
            process_cpu_seconds=(end.ru_utime + end.ru_stime) - (start.ru_utime + start.ru_stime),
            peak_rss_bytes=end.ru_maxrss * 1024,
        )
        print(json.dumps(result))
    else:
        main()
