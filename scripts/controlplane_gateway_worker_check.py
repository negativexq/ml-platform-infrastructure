#!/usr/bin/env python3
"""Run disposable 429-only gateway worker probes on the explicit acceptance lab.

Preserves installed deployment settings and endpoint limits. Requires its existing
comparison loadgen image, PostgreSQL migration secret and live Collector/Prometheus.
Creates then revokes a distinct API key; removes only uniquely named probe resources.
"""

import argparse
import json
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
KUBE = [
    "kubectl",
    "--kubeconfig",
    "/tmp/mlp-acceptance-kubeconfig",
    "--context",
    "kind-mlp-acceptance",
    "-n",
    "mlp-system",
]


def run(args, **kwargs):
    return subprocess.check_output(KUBE + args, text=True, **kwargs)


def apply(obj):
    run(["apply", "-f", "-"], input=json.dumps(obj))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases",
        nargs="+",
        choices=["12:15", "24:15", "24:30"],
        default=["12:15", "24:15", "24:30", "24:15", "12:15"],
    )
    parser.add_argument(
        "--profile-cpu",
        action="store_true",
        help="Profile the middle case; brackets remain unprofiled",
    )
    parser.add_argument(
        "--otel-modes",
        nargs="+",
        choices=["on", "off"],
        help="Per-case SDK mode; defaults to on for every case",
    )
    parser.add_argument(
        "--telemetry-variants",
        nargs="+",
        choices=["full", "sql-off", "trace5", "lean-labels", "normal-interval", "normal", "off"],
        help="Per-case cumulative telemetry variants; requires a profile-capable image",
    )
    parser.add_argument("--rps", type=int, nargs="+", help="Per-case offered RPS; default 500")
    parser.add_argument("--duration-seconds", type=int, default=10)
    parser.add_argument(
        "--connection-modes",
        nargs="+",
        choices=["baseline", "short-idle", "no-keepalive"],
        help="Per-case Go transport experiment; same connection diagnostics in every arm",
    )
    parser.add_argument("--queue-size", type=int, default=5000)
    parser.add_argument(
        "--loadgen-image", help="Optional comparison image with Go generator diagnostics"
    )
    parser.add_argument(
        "--gateway-runtimes",
        nargs="+",
        choices=["python", "go"],
        help="Per-case runtime; Go requires explicit candidate image and normal profile",
    )
    parser.add_argument("--go-gateway-image")
    parser.add_argument(
        "--go-server-idle-timeout-seconds",
        type=int,
        default=5,
        help=("Explicit Go probe idle timeout; default 5 matches historical Python "
              "comparisons, production defaults to 60"),
    )
    parser.add_argument(
        "--contract-smoke",
        action="store_true",
        help="Verify real function forwarding and refusals outside timed load",
    )
    parser.add_argument("--probe-image", help="Optional disposable profiler image digest")
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "docs/evidence/live-2026-10-07/observability/worker-experiment.json",
    )
    args = parser.parse_args()
    if not 1 <= args.go_server_idle_timeout_seconds <= 600:
        parser.error("Go server idle timeout must be 1–600 seconds")
    if args.profile_cpu and (
        len(args.cases) != 3 or len(set(args.cases)) != 1 or not args.probe_image
    ):
        parser.error("CPU profiling requires three identical cases and a disposable profiler image")
    if not 1 <= args.duration_seconds <= 600:
        parser.error("duration must be 1–600 seconds")
    if args.profile_cpu and args.duration_seconds != 10:
        parser.error("CPU profiling remains limited to ten seconds")
    rates = args.rps or [500] * len(args.cases)
    if len(rates) != len(args.cases) or min(rates) <= 0 or args.queue_size <= 0:
        parser.error("requires one positive RPS per case and a positive queue size")
    runtimes = args.gateway_runtimes or ["python"] * len(args.cases)
    if len(runtimes) != len(args.cases):
        parser.error("requires one gateway runtime per case")
    if "go" in runtimes and (
        not args.go_gateway_image
        or args.profile_cpu
        or not args.telemetry_variants
        or any(v != "normal" for v in args.telemetry_variants)
    ):
        parser.error("Go PoC requires explicit image, normal profile and no CPU profiler")
    connections = args.connection_modes or [None] * len(args.cases)
    if len(connections) != len(args.cases):
        parser.error("requires one connection mode per case")
    variants = args.telemetry_variants or [None] * len(args.cases)
    if len(variants) != len(args.cases):
        parser.error("requires one telemetry variant per case")
    if args.telemetry_variants and not args.probe_image and any(r != "go" for r in runtimes):
        parser.error("telemetry variants require an explicit profile-capable --probe-image")
    if args.telemetry_variants and (args.otel_modes or args.profile_cpu):
        parser.error("variants cannot be combined with --otel-modes or CPU profiling")
    modes = args.otel_modes or ["off" if v == "off" else "on" for v in variants]
    if len(modes) != len(args.cases):
        parser.error("--otel-modes must have one mode per case")
    if args.profile_cpu and "off" in modes:
        parser.error("Keep CPU profiling separate from OTel A/B")
    name = "worker-probe-" + secrets.token_hex(4)
    project, endpoint = "acceptance-ca9287f7c0", "function"
    output = args.out
    if output.exists():
        raise RuntimeError("evidence already exists; preserve earlier results")
    jobs, pods = [], []
    key_id = None
    report = {
        "scope": "ARM64 single-node 429-only Go load; gateway 1 CPU; per-case OTel SDK mode",
        "samples": [],
        "passed": False,
    }
    with tempfile.TemporaryDirectory(prefix="mlp-worker-probe-") as temporary:
        log = (Path(temporary) / "forward.log").open("w")
        forward = subprocess.Popen(
            KUBE + ["port-forward", "service/mlp-controlplane-api", "18895:80"],
            stdout=log,
            stderr=log,
        )
        try:
            with httpx.Client(base_url="http://127.0.0.1:18895", timeout=15) as api:
                for _ in range(50):
                    try:
                        if api.get("/readyz").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.2)
                response = api.post(
                    f"/projects/{project}/api-keys",
                    json={"name": name, "endpoints": [endpoint], "units_per_minute": 600},
                )
                response.raise_for_status()
                key = response.json()
                key_id = key["key"]["key_id"]
                apply(
                    {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": {"name": name},
                        "stringData": {"token": key["secret"]},
                    }
                )
                scripts = {
                    p: (ROOT / "scripts" / p).read_text()
                    for p in [
                        "controlplane_gateway_worker_probe.py",
                        "controlplane_gateway_worker_sample.py",
                        "controlplane_limiter_check.py",
                    ]
                }
                apply(
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": name},
                        "data": scripts,
                    }
                )
                template = json.loads(
                    run(["get", "deployment/mlp-controlplane-gateway", "-o", "json"])
                )["spec"]["template"]
                for index, (workers, pool) in enumerate(
                    [tuple(map(int, case.split(":"))) for case in args.cases]
                ):
                    mode = modes[index]
                    pod_name = f"{name}-{index}"
                    pod = {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "metadata": {"name": pod_name, "labels": {"mlp.io/acceptance-probe": name}},
                        "spec": json.loads(json.dumps(template["spec"])),
                    }
                    if args.contract_smoke:
                        # Allow serving ingress; keep the installed Service selector unmatched.
                        pod["metadata"]["labels"]["app.kubernetes.io/name"] = "mlp-gateway"
                    pod["spec"].pop("affinity", None)
                    pod["spec"].pop("topologySpreadConstraints", None)
                    pod["spec"]["restartPolicy"] = "Never"
                    pod["spec"]["terminationGracePeriodSeconds"] = 5
                    pod["spec"].setdefault("volumes", []).append(
                        {"name": "probe", "configMap": {"name": name}}
                    )
                    container = pod["spec"]["containers"][0]
                    if args.probe_image:
                        container["image"] = args.probe_image
                    container["command"] = ["python", "/probe/controlplane_gateway_worker_probe.py"]
                    if runtimes[index] == "go":
                        container["image"] = args.go_gateway_image
                        container["command"] = ["/mlp-gateway-go"]
                        overrides = {
                            "CP_GATEWAY_IDLE_TIMEOUT": f"{args.go_server_idle_timeout_seconds}s",
                            "CP_GATEWAY_WORKERS": str(workers),
                            "CP_GATEWAY_DB_POOL_CAPACITY": str(pool),
                        }
                        container["env"] = [
                            e for e in container["env"] if e["name"] not in overrides
                        ]
                        container["env"] += [{"name": k, "value": v} for k, v in overrides.items()]
                    container.pop("args", None)
                    container.setdefault("volumeMounts", []).append(
                        {"name": "probe", "mountPath": "/probe", "readOnly": True}
                    )
                    container["env"] = [
                        e for e in container["env"] if e["name"] != "OTEL_SDK_DISABLED"
                    ]
                    if variants[index] is not None:
                        variant = variants[index]
                        telemetry_env = {
                            "CP_GATEWAY_OBSERVABILITY_PROFILE": "normal"
                            if variant == "normal"
                            else "acceptance",
                            "CP_GATEWAY_SQL_TRACING": "true" if variant == "full" else "false",
                            "CP_GATEWAY_TRACE_SAMPLE_RATE": "0.01"
                            if variant in {"full", "sql-off", "normal", "off"}
                            else "0.05",
                            "CP_GATEWAY_REQUEST_CALLER_LABEL": "true"
                            if variant in {"full", "sql-off", "trace5"}
                            else "false",
                            "CP_GATEWAY_HTTP_METRICS": "false" if variant == "normal" else "true",
                            "CP_GATEWAY_HTTP_OPERATION_SPANS": "false"
                            if variant == "normal"
                            else "true",
                            "CP_GATEWAY_LATENCY_SAMPLE_RATE": "0.2" if variant == "normal" else "1",
                            "CP_GATEWAY_METRIC_EXPORT_INTERVAL_MS": "30000"
                            if variant in {"normal-interval", "normal"}
                            else "1000",
                        }
                        container["env"] = [
                            e for e in container["env"] if e["name"] not in telemetry_env
                        ]
                        container["env"] += [
                            {"name": k, "value": v} for k, v in telemetry_env.items()
                        ]
                    container["env"] += [
                        {
                            "name": "OTEL_SDK_DISABLED",
                            "value": "true" if mode == "off" else "false",
                        },
                        {"name": "CP_GATEWAY_PROBE_ENABLED", "value": "true"},
                        {"name": "PROBE_WORKERS", "value": str(workers)},
                        {"name": "PROBE_POOL_CAPACITY", "value": str(pool)},
                        {
                            "name": "PROBE_CPU_PROFILE",
                            "value": "true" if args.profile_cpu else "false",
                        },
                    ]
                    pods.append(pod_name)
                    apply(pod)
                    run(["wait", "--for=condition=Ready", "pod/" + pod_name, "--timeout=90s"])
                    state = json.loads(run(["get", "pod/" + pod_name, "-o", "json"]))
                    ip, uid = state["status"]["podIP"], state["metadata"]["uid"]
                    job_name = pod_name + "-load"
                    jobs.append(job_name)
                    sample_args = [
                        "python",
                        "/scripts/controlplane_gateway_worker_sample.py",
                        "--url",
                        f"http://{ip}:8081/v1/{project}/{endpoint}/invoke",
                        "--runtime-url",
                        f"http://{ip}:8082",
                        "--instance",
                        uid,
                        "--project",
                        project,
                        "--endpoint",
                        endpoint,
                        "--caller",
                        name,
                        "--workers",
                        str(workers),
                        "--pool",
                        str(pool),
                    ]
                    sample_args += [
                        "--otel-mode",
                        mode,
                        "--rps",
                        str(rates[index]),
                        "--queue-size",
                        str(args.queue_size),
                        "--duration-seconds",
                        str(args.duration_seconds),
                    ]
                    if args.contract_smoke:
                        sample_args.append("--contract-smoke")
                    if connections[index] is not None:
                        sample_args += ["--connection-mode", connections[index]]
                    if args.profile_cpu and index == 1:
                        sample_args.append("--profile-cpu")
                    job = {
                        "apiVersion": "batch/v1",
                        "kind": "Job",
                        "metadata": {"name": job_name},
                        "spec": {
                            "backoffLimit": 0,
                            "activeDeadlineSeconds": args.duration_seconds + 120,
                            "template": {
                                "spec": {
                                    "restartPolicy": "Never",
                                    "automountServiceAccountToken": False,
                                    "securityContext": {
                                        "runAsNonRoot": True,
                                        "runAsUser": 10001,
                                        "seccompProfile": {"type": "RuntimeDefault"},
                                    },
                                    "volumes": [{"name": "scripts", "configMap": {"name": name}}],
                                    "containers": [
                                        {
                                            "name": "loadgen",
                                            "image": args.loadgen_image
                                            or (
                                                "localhost:5201/mlp-loadgen@sha256:"
                                                "40973a26fdf21516f4730ff80cfa30b9ef76b5a158d4d4eedba1615eb440d284"
                                            ),
                                            "command": sample_args,
                                            "resources": {
                                                "requests": {"cpu": "500m", "memory": "128Mi"},
                                                "limits": {"cpu": "2", "memory": "384Mi"},
                                            },
                                            "securityContext": {
                                                "allowPrivilegeEscalation": False,
                                                "readOnlyRootFilesystem": True,
                                                "capabilities": {"drop": ["ALL"]},
                                            },
                                            "volumeMounts": [
                                                {
                                                    "name": "scripts",
                                                    "mountPath": "/scripts",
                                                    "readOnly": True,
                                                }
                                            ],
                                            "env": [
                                                {
                                                    "name": "CP_ACCEPTANCE_GATEWAY_TOKEN",
                                                    "valueFrom": {
                                                        "secretKeyRef": {
                                                            "name": name,
                                                            "key": "token",
                                                        }
                                                    },
                                                },
                                                {
                                                    "name": "CP_ACCEPTANCE_ADMIN_DATABASE_URL",
                                                    "valueFrom": {
                                                        "secretKeyRef": {
                                                            "name": "mlp-controlplane-db-migration",
                                                            "key": "url",
                                                        }
                                                    },
                                                },
                                            ],
                                        }
                                    ],
                                }
                            },
                        },
                    }
                    apply(job)
                    try:
                        run(
                            [
                                "wait",
                                "--for=condition=complete",
                                "job/" + job_name,
                                f"--timeout={args.duration_seconds + 110}s",
                            ]
                        )
                    except subprocess.CalledProcessError:
                        failure = Path("/tmp/mlp-worker-probe-private/failure.log")
                        failure.parent.mkdir(mode=0o700, exist_ok=True)
                        failure.write_text(run(["logs", "job/" + job_name]))
                        failure.chmod(0o600)
                        raise RuntimeError(
                            "probe job failed; private diagnostics retained"
                        ) from None
                    lines = run(["logs", "job/" + job_name]).splitlines()
                    result = json.loads(lines[-1])
                    if variants[index] is not None:
                        field_env = {
                            "sql_tracing": "CP_GATEWAY_SQL_TRACING",
                            "latency_sample_rate": "CP_GATEWAY_LATENCY_SAMPLE_RATE",
                            "trace_sample_rate": "CP_GATEWAY_TRACE_SAMPLE_RATE",
                            "request_caller_label": "CP_GATEWAY_REQUEST_CALLER_LABEL",
                            "native_http_metrics": "CP_GATEWAY_HTTP_METRICS",
                            "operation_spans": "CP_GATEWAY_HTTP_OPERATION_SPANS",
                            "export_interval_ms": "CP_GATEWAY_METRIC_EXPORT_INTERVAL_MS",
                        }
                        for field, env_name in field_env.items():
                            actual = result["telemetry_profile"].get(field)
                            if actual is None or (
                                str(actual).lower() != telemetry_env[env_name]
                                if isinstance(actual, bool)
                                else float(actual) != float(telemetry_env[env_name])
                            ):
                                raise RuntimeError(
                                    "probe image/profile does not match requested variant"
                                )
                    result.update(
                        index=index,
                        instance=uid,
                        image=container["image"],
                        telemetry_variant=variants[index],
                        connection_mode=connections[index],
                        gateway_runtime=runtimes[index],
                        loadgen_image=job["spec"]["template"]["spec"]["containers"][0]["image"],
                    )
                    report["samples"].append(result)
                    output.write_text(json.dumps(report, indent=2) + "\n")
                    print(
                        json.dumps(
                            {
                                "otel_mode": mode,
                                "workers": workers,
                                "pool": pool,
                                "rps": result["completed_rps"],
                                "request_p95_ms": result["request_latency_ms"]["p95"],
                                "phases": result["phases"],
                                "cpu": result["gateway_cpu_stat_delta"],
                            }
                        ),
                        flush=True,
                    )
                    run(["delete", "pod/" + pod_name, "job/" + job_name, "--wait=true"])
                report["completed"] = True
                report["passed"] = all(
                    r["availability_passed"]
                    and r["integrity_passed"]
                    and (r.get("capacity_passed", True) if args.duration_seconds >= 60 else True)
                    for r in report["samples"]
                )
        finally:
            if key_id:
                with httpx.Client(base_url="http://127.0.0.1:18895", timeout=15) as api:
                    response = api.delete(f"/projects/{project}/api-keys/{key_id}")
                    report["key_revoked"] = response.status_code in [200, 204]
            for kind, names in [
                ("pod", pods),
                ("job", jobs),
                ("configmap", [name]),
                ("secret", [name]),
            ]:
                subprocess.run(
                    KUBE + ["delete", *[kind + "/" + n for n in names], "--ignore-not-found"],
                    capture_output=True,
                )
            forward.terminate()
            forward.wait(timeout=10)
            log.close()
            output.write_text(json.dumps(report, indent=2) + "\n")

    if not report.get("passed"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
