#!/usr/bin/env python3
"""Read-only trace/counter-query verification on the explicit acceptance lab."""

import argparse
import json
import subprocess
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

import httpx

root = Path(__file__).resolve().parents[1] / "docs/evidence/live-2026-10-07/observability"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--report", type=Path, default=root / "gateway-hotpath-ab.json")
parser.add_argument(
    "--allow-availability-failure",
    action="store_true",
    help="Validate telemetry on accounted failed traffic; never changes its gate",
)
parser.add_argument("--out", type=Path, default=root / "gateway-hotpath-telemetry-check.json")
args = parser.parse_args()
if args.out.exists():
    parser.error("evidence exists; use a new output path")
report = json.loads(args.report.read_text())
assert (
    report["completed"]
    and (report["passed"] or args.allow_availability_failure)
    and report["key_revoked"]
)
assert all(
    (s["completed"] + s["transport_errors"] if args.allow_availability_failure else s["completed"])
    == s["offered_rps"] * s.get("duration_seconds", 10)
    for s in report["samples"]
)
assert all(
    (s["request_accounting_passed"] if args.allow_availability_failure else s["integrity_passed"])
    and not s["queue_dropped"]
    for s in report["samples"]
)
base = [
    "kubectl",
    "--kubeconfig",
    "/tmp/mlp-acceptance-kubeconfig",
    "--context",
    "kind-mlp-acceptance",
    "-n",
    "observability",
]
checks = {
    "trace_checks": [],
    "promql_checks": [],
    "upstream_trace_checks": [],
    "passed": False,
    "traffic_gate_passed": report["passed"],
    "scope": "Read-only telemetry verification; does not override traffic gate",
}
expected_traces = sum(bool(s["warmup_trace_id"]) for s in report["samples"])
processes = []
with tempfile.TemporaryDirectory() as tmp, ExitStack() as handles:
    try:
        for service, local, remote in [("tempo", 18896, 3200), ("prometheus", 18897, 9090)]:
            log = handles.enter_context(open(Path(tmp) / service, "w"))
            processes.append(
                (
                    subprocess.Popen(
                        base + ["port-forward", "service/" + service, f"{local}:{remote}"],
                        stdout=log,
                        stderr=log,
                    ),
                    log,
                )
            )
        with httpx.Client(timeout=15) as client:
            for _ in range(50):
                try:
                    if (
                        client.get("http://127.0.0.1:18896/ready").status_code == 200
                        and client.get("http://127.0.0.1:18897/-/ready").status_code == 200
                    ):
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.2)
            for sample in report["samples"]:
                if not sample["warmup_trace_id"]:
                    continue
                r = client.get("http://127.0.0.1:18896/api/traces/" + sample["warmup_trace_id"])
                r.raise_for_status()
                scoped = [
                    (scope.get("scope", {}).get("name", ""), span)
                    for batch in r.json().get("batches", [])
                    for scope in batch.get("scopeSpans", [])
                    for span in scope.get("spans", [])
                ]
                sql = sum("sqlalchemy" in scope for scope, span in scoped)
                server = sum(
                    str(span.get("kind")) in {"2", "SPAN_KIND_SERVER"} for scope, span in scoped
                )
                expect = sample["telemetry_profile"]["sql_tracing"]
                checks["trace_checks"].append(
                    {
                        "index": sample["index"],
                        "variant": sample["telemetry_variant"],
                        "trace_id": sample["warmup_trace_id"],
                        "sql_span_count": sql,
                        "server_span_count": server,
                        "scope_names": sorted(set(scope for scope, span in scoped)),
                        "passed": server >= 1 and (sql > 0 if expect else sql == 0),
                    }
                )
            for sample in report["samples"]:
                smoke_trace = sample.get("upstream_smoke_trace_id")
                if not smoke_trace:
                    continue
                response = client.get("http://127.0.0.1:18896/api/traces/" + smoke_trace)
                response.raise_for_status()
                spans = [
                    span
                    for batch in response.json().get("batches", [])
                    for scope in batch.get("scopeSpans", [])
                    for span in scope.get("spans", [])
                ]
                server_ids = {
                    span.get("spanId")
                    for span in spans
                    if str(span.get("kind")) in {"2", "SPAN_KIND_SERVER"}
                }
                linked_clients = sum(
                    str(span.get("kind")) in {"3", "SPAN_KIND_CLIENT"}
                    and span.get("parentSpanId") in server_ids
                    for span in spans
                )
                checks["upstream_trace_checks"].append(
                    {
                        "index": sample["index"],
                        "runtime": sample.get("gateway_runtime", "python"),
                        "trace_id": smoke_trace,
                        "linked_upstream_client_spans": linked_clients,
                        "passed": bool(server_ids) and linked_clients >= 1,
                    }
                )
            selector = 'project="acceptance-ca9287f7c0",endpoint="function"'
            for code in ["4..", "5.."]:
                q = (
                    f"60 * sum by (caller) (rate(mlp_gateway_usage_requests_total"
                    f'{{{selector},code=~"{code}"}}[5m]) or rate(mlp_gateway_requests_total'
                    f'{{{selector},caller!="",code=~"{code}"}}[5m]))'
                )
                r = client.get("http://127.0.0.1:18897/api/v1/query", params={"query": q})
                r.raise_for_status()
                checks["promql_checks"].append(
                    {"query": q, "passed": r.json()["status"] == "success"}
                )
            latest = next(
                s for s in reversed(report["samples"]) if s["telemetry_variant"] == "normal"
            )
            checks["counter_schema_checks"] = []
            for metric, has_caller in [
                ("mlp_gateway_requests_total", False),
                ("mlp_gateway_usage_requests_total", True),
            ]:
                r = client.get(
                    "http://127.0.0.1:18897/api/v1/query_range",
                    params={
                        "query": metric + '{instance="' + latest["instance"] + '"}',
                        "start": latest.get("load_started_at_epoch", time.time() - 890) - 10,
                        "end": latest.get("load_finished_at_epoch", time.time() - 20) + 20,
                        "step": 10,
                    },
                )
                r.raise_for_status()
                series = r.json()["data"]["result"]
                peak = sum(max(float(v) for _, v in s["values"]) for s in series)
                checks["counter_schema_checks"].append(
                    {
                        "metric": metric,
                        "peak_count_including_100_warmups_and_contract_smoke": peak,
                        "contract_smoke_count": sum(
                            c["status"] >= 400 if has_caller else True
                            for c in latest.get("contract_checks", [])
                        ),
                        "has_caller": has_caller,
                        "passed": peak
                        == latest["completed"]
                        + 100
                        + sum(
                            c["status"] >= 400 if has_caller else True
                            for c in latest.get("contract_checks", [])
                        )
                        and all(("caller" in s["metric"]) == has_caller for s in series),
                    }
                )
            checks["passed"] = (
                all(
                    c["passed"]
                    for c in checks["trace_checks"]
                    + checks["promql_checks"]
                    + checks["counter_schema_checks"]
                    + checks["upstream_trace_checks"]
                )
                and len(checks["trace_checks"]) == expected_traces
            )
    finally:
        for process, log in processes:
            process.terminate()
            process.wait(timeout=10)
            log.close()
args.out.write_text(json.dumps(checks, indent=2) + "\n")
print(json.dumps(checks, indent=2))
assert checks["passed"]
