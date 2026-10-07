#!/usr/bin/env python3
"""Read-only acceptance-lab Tempo query/memory check; never writes raw trace payloads."""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx


def spans(payload):
    return [
        span
        for batch in payload.get("batches", [])
        for scope in batch.get("scopeSpans", [])
        for span in scope.get("spans", [])
    ]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tempo-url", required=True)
    p.add_argument("--new-traces", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--repeat", type=int, default=100)
    p.add_argument("--concurrency", type=int, default=2)
    args = p.parse_args()
    if args.out.exists() or not 1 <= args.repeat <= 200 or not 1 <= args.concurrency <= 4:
        p.error("requires new evidence path and bounded query count/concurrency")
    report = {
        "passed": False,
        "concurrency": args.concurrency,
        "repeat": args.repeat,
        "scope": "historical single-gateway load-window search/read plus newly exported API traces",
        "historical_window": [1791332670, 1791332688],
        "queries": [],
        "new_traces": [],
    }
    try:
        with httpx.Client(base_url=args.tempo_url, timeout=30) as client:
            r = client.get("/ready")
            r.raise_for_status()
            r = client.get(
                "/api/search",
                params={
                    "start": 1791332670,
                    "end": 1791332688,
                    "tags": "service.name=mlp-gateway",
                    "limit": 100,
                },
            )
            r.raise_for_status()
            ids = [t["traceID"] for t in r.json().get("traces", [])]
            if not ids:
                raise RuntimeError("restored historical window contains no traces")
            report["historical_trace_count"] = len(ids)

            def query(index):
                trace_id = ids[index % len(ids)]
                start = time.perf_counter()
                try:
                    r = client.get("/api/traces/" + trace_id)
                    payload = r.json() if r.status_code == 200 else {}
                    count = len(spans(payload))
                    return {
                        "trace_id": trace_id,
                        "status": r.status_code,
                        "span_count": count,
                        "duration_ms": (time.perf_counter() - start) * 1000,
                        "passed": r.status_code == 200 and count > 0,
                    }
                except (httpx.HTTPError, ValueError) as exc:
                    return {"trace_id": trace_id, "error_type": type(exc).__name__, "passed": False}

            with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
                report["queries"] = list(executor.map(query, range(args.repeat)))
            for item in json.loads(args.new_traces.read_text()):
                r = client.get("/api/traces/" + item["trace_id"])
                r.raise_for_status()
                names = [s["name"] for s in spans(r.json())]
                report["new_traces"].append(
                    {
                        "trace_id": item["trace_id"],
                        "span_names": names,
                        "passed": "GET /projects" in names and any("SELECT" in n for n in names),
                    }
                )
            report["passed"] = all(q["passed"] for q in report["queries"] + report["new_traces"])
    finally:
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "historical_queries": len(report["queries"]),
                "new_traces": len(report["new_traces"]),
            }
        )
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
