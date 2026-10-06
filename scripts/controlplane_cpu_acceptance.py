#!/usr/bin/env python3
"""Explicit live CPU lifecycle gate. Default prints the plan and changes nothing."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from controlplane.adapters.metrics import PrometheusMetricsProvider
from controlplane.application.providers import RevisionMetrics

PHASES = [
    "project/RBAC",
    "training/MLflow/discovery",
    "CPU serving/gateway",
    "canary/metrics",
    "function/credential rotation",
    "same-revision drift repair",
    "scale-to-zero/reactivation",
]


def _poll(
    read: Callable[[], Any], ready: Callable[[Any], bool], label: str, *, timeout: float
) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = read()
        if ready(value):
            return value
        status = value.get("status") if isinstance(value, dict) else None
        if isinstance(status, str) and status in {"FAILED", "REJECTED"}:
            raise RuntimeError(label + " failed")
        time.sleep(2)
    raise RuntimeError(label + " deadline exceeded")


def _repaired_backend_matches(
    service: dict[str, Any],
    read_revision: Callable[[str], dict[str, Any]],
    old_apply: str,
    old_backend: str,
) -> bool:
    apply_id = service["spec"]["predictor"]["annotations"]["mlp.io/apply-id"]
    backend = (
        service.get("status", {})
        .get("components", {})
        .get("predictor", {})
        .get("latestReadyRevision")
    )
    if apply_id == old_apply or backend in {None, old_backend}:
        return False
    revision = read_revision(backend)
    return bool(revision["metadata"].get("annotations", {}).get("mlp.io/apply-id") == apply_id)


def _retain_metric_evidence(
    measured: dict[str, Any], backend: str, revision: int, sample: RevisionMetrics
) -> None:
    if not sample.requests or sample.p95_latency_ms is None or sample.error_rate is None:
        return
    measured[backend] = {
        "platform_revision": revision,
        "observed_at": datetime.now(UTC).isoformat(),
        **asdict(sample),
    }


def execute(args: argparse.Namespace) -> None:
    token = os.environ.get("CP_ACCEPTANCE_API_TOKEN")
    if not token:
        raise ValueError("CP_ACCEPTANCE_API_TOKEN is required")
    project = "acceptance-" + uuid4().hex[:10]
    namespace = "mlp-" + project
    report: dict[str, Any] = {
        "project": project,
        "context": args.context,
        "passed": False,
        "phases": [],
        "source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "source_clean": not subprocess.check_output(
            ["git", "status", "--porcelain"], text=True
        ).strip(),
    }
    started = time.monotonic()
    with (
        httpx.Client(
            base_url=args.api.rstrip("/"), headers={"authorization": "Bearer " + token}, timeout=30
        ) as api,
        httpx.Client(base_url=args.gateway.rstrip("/"), timeout=120) as gateway,
    ):

        def call(method: str, path: str, body: Any = None) -> Any:
            response = api.request(method, path, json=body)
            if response.status_code >= 400:
                raise RuntimeError(f"API operation {method} {path} failed ({response.status_code})")
            return response.json() if response.content else None

        def kubectl(*parts: str) -> str:
            result = subprocess.run(
                ["kubectl", "--context", args.context, *parts],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode:
                raise RuntimeError("Kubernetes acceptance operation failed")
            return result.stdout

        def poll(read: Callable[[], Any], ready: Callable[[Any], bool], label: str) -> Any:
            return _poll(read, ready, label, timeout=args.timeout)

        def record(phase: str, **evidence: Any) -> None:
            report["phases"].append(
                {"phase": phase, "elapsed_seconds": time.monotonic() - started, **evidence}
            )
            print("PASS", phase, flush=True)

        base = "/projects/" + project
        try:
            created_project = call("POST", "/projects", {"name": project})
            report["project_id"] = created_project["id"]
            poll(
                lambda: call("GET", "/projects/" + created_project["id"]),
                lambda p: p["status"] == "READY",
                "project",
            )
            for resource in (
                "resourcequota/mlp-quota",
                "role/mlp-workflow-executor",
                "rolebinding/mlp-api-secrets",
                "rolebinding/mlp-api-workload-reader",
            ):
                kubectl("-n", namespace, "get", resource)
            api_sa = (
                f"system:serviceaccount:{args.system_namespace}:{args.release}-controlplane-api"
            )
            for resource in (
                "secrets",
                "pods",
                "pods/log",
                "workflows.argoproj.io",
                "inferenceservices.serving.kserve.io",
                "revisions.serving.knative.dev",
            ):
                for ns, expected in (
                    (namespace, "yes"),
                    (args.system_namespace, "no"),
                    ("kube-system", "no"),
                ):
                    result = subprocess.run(
                        [
                            "kubectl",
                            "--context",
                            args.context,
                            "auth",
                            "can-i",
                            "get",
                            resource,
                            "-n",
                            ns,
                            "--as",
                            api_sa,
                        ],
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    if result.stdout.strip() != expected:
                        raise RuntimeError(f"API RBAC scope mismatch: {resource} in {ns}")
            admission = subprocess.run(
                [
                    sys.executable,
                    "scripts/controlplane_admission_check.py",
                    "--context",
                    args.context,
                    "--release",
                    args.release,
                    "--system-namespace",
                    args.system_namespace,
                    "--project-namespace",
                    namespace,
                ],
                capture_output=True,
                text=True,
                timeout=360,
                check=False,
            )
            if admission.returncode:
                raise RuntimeError("Reconciler admission boundary gate failed")
            record(PHASES[0], admission_checked=True)
            pulls = []
            if args.registry_secret:
                call(
                    "POST", base + "/secrets/registry", json.loads(args.registry_secret.read_text())
                )
                pulls = ["registry"]
            storage = None
            job_secrets: dict[str, Any] = {"image_pull_secrets": pulls}
            training_env = json.loads(args.training_env.read_text()) if args.training_env else {}
            if args.storage_secret:
                storage = json.loads(args.storage_secret.read_text())
                call("POST", base + "/secrets/artifact-storage", storage)
                job_secrets["env"] = {
                    name: {"name": "artifact-storage", "key": name}
                    for name in storage["values"]
                    if name.startswith("AWS_")
                }
                annotations = storage.get("annotations") or {}
                endpoint = annotations.get("serving.kserve.io/s3-endpoint")
                if endpoint:
                    protocol = (
                        "http"
                        if annotations.get("serving.kserve.io/s3-usehttps") == "0"
                        else "https"
                    )
                    training_env["MLFLOW_S3_ENDPOINT_URL"] = protocol + "://" + endpoint
            model = call(
                "POST",
                base + "/models",
                {
                    "name": "scorer",
                    "thresholds": {"r2": {"min": 0.9}},
                    "secret_refs": {"storage_secret": "artifact-storage"} if storage else {},
                },
            )
            call(
                "POST",
                base + "/jobs",
                {
                    "name": "train",
                    "image": args.training_image,
                    "command": ["python", "scripts/train.py", "--register", model["registry_name"]],
                    "env": training_env,
                    "secret_refs": job_secrets,
                },
            )
            call(
                "POST",
                base + "/pipelines",
                {"name": "train", "steps": [{"name": "train", "job": "train"}]},
            )

            def train() -> dict[str, Any]:
                run = call("POST", base + "/pipelines/train/runs", {})
                poll(
                    lambda: call("GET", "/pipeline-runs/" + run["id"]),
                    lambda r: r["status"] == "SUCCEEDED",
                    "training",
                )
                versions = poll(
                    lambda: call("GET", base + "/models/scorer/versions")["items"],
                    lambda vs: any(v["source_pipeline_run_id"] == run["id"] for v in vs),
                    "automatic discovery",
                )
                version = next(v for v in versions if v["source_pipeline_run_id"] == run["id"])
                evaluation = call("POST", "/model-versions/" + version["id"] + "/evaluate")
                if evaluation["status"] != "CANDIDATE":
                    raise RuntimeError("training version did not pass evaluation")
                assert isinstance(version, dict)
                return version

            first = train()
            record(PHASES[1], version_id=first["id"])
            call("POST", base + "/deployments", {"name": "scorer"})
            call(
                "POST",
                base + "/deployments/scorer/revisions",
                {"model": "scorer", "version": first["version"]},
            )
            poll(
                lambda: call("GET", base + "/endpoints/scorer"),
                lambda e: e["status"] == "READY",
                "CPU serving",
            )

            def open_endpoint(name: str) -> str:
                call(
                    "PATCH",
                    base + "/endpoints/" + name,
                    {
                        "exposure": "public",
                        "limits": {"units_per_minute": 100000, "timeout_seconds": 120},
                    },
                )
                key = call("POST", base + "/api-keys", {"name": name, "endpoints": [name]})[
                    "secret"
                ]
                assert isinstance(key, str)
                return key

            model_key = open_endpoint("scorer")

            def invoke(name: str, key: str, body: dict[str, Any], operation: str) -> dict[str, Any]:
                response = gateway.post(
                    f"/v1/{project}/{name}/{operation}",
                    json=body,
                    headers={"authorization": "Bearer " + key},
                )
                if response.status_code != 200:
                    raise RuntimeError("gateway acceptance call failed")
                result: dict[str, Any] = response.json()
                return result

            invoke("scorer", model_key, {"instances": [[1, 2, 3]]}, "predict")
            record(PHASES[2])
            second = train()
            rollout = call(
                "POST",
                base + "/deployments/scorer/rollouts",
                {
                    "model": "scorer",
                    "version": second["version"],
                    "steps": [10, 100],
                    "gate": {
                        "step_seconds": 60,
                        "min_requests": 10,
                        "max_error_rate": 0.05,
                        "max_p95_latency_ms": 5000,
                    },
                },
            )
            seen_backends: set[str] = set()
            metrics = PrometheusMetricsProvider(args.prometheus)
            measured: dict[str, Any] = {}
            backend_numbers: dict[str, int] = {}
            next_metrics_at = 0.0
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                invoke("scorer", model_key, {"instances": [[1, 2, 3]]}, "predict")
                isvc = json.loads(kubectl("-n", namespace, "get", "isvc/scorer", "-o", "json"))
                component = isvc.get("status", {}).get("components", {}).get("predictor", {})
                for field in ("latestReadyRevision", "previousRolledoutRevision"):
                    if component.get(field):
                        seen_backends.add(component[field])
                current = call("GET", "/rollouts/" + rollout["id"])
                if time.monotonic() >= next_metrics_at or current["status"] == "SUCCEEDED":
                    for backend_name in seen_backends:
                        if backend_name not in backend_numbers:
                            backend = json.loads(
                                kubectl(
                                    "-n", namespace, "get", "revision/" + backend_name, "-o", "json"
                                )
                            )
                            backend_numbers[backend_name] = int(
                                backend["metadata"]["annotations"]["mlp.io/revision"]
                            )
                        number = backend_numbers[backend_name]
                        _retain_metric_evidence(
                            measured,
                            backend_name,
                            number,
                            metrics.revision_metrics(namespace + "/scorer", number, backend_name),
                        )
                    next_metrics_at = time.monotonic() + 5
                if current["status"] in {"SUCCEEDED", "ROLLED_BACK"}:
                    if current["status"] != "SUCCEEDED":
                        raise RuntimeError(
                            "healthy candidate rolled back; inspect metric attribution"
                        )
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("canary deadline exceeded")
            if len(seen_backends) < 2:
                raise RuntimeError("stable/candidate backend evidence missing")
            if seen_backends - measured.keys():
                raise RuntimeError("backend-scoped metric evidence is missing")
            if len({m["platform_revision"] for m in measured.values()}) < 2:
                raise RuntimeError("stable/candidate metric identity was not distinct")
            record(
                PHASES[3],
                rollout_id=rollout["id"],
                backend_revisions=sorted(seen_backends),
                metrics=measured,
                limitation="Healthy gate only; adversarial metric-label test remains separate",
            )
            value = secrets.token_hex(24)
            credential = call(
                "POST", base + "/secrets/function-credential", {"values": {"value": value}}
            )
            call(
                "POST",
                base + "/models",
                {
                    "name": "function",
                    "kind": "function",
                    "function": {"min_scale": 0, "max_scale": 2, "readiness_path": "/healthz"},
                    "secret_refs": {
                        "env": {"TEST_CREDENTIAL": {"name": "function-credential", "key": "value"}}
                    },
                },
            )
            version = call("POST", base + "/models/function/images", {"image": args.function_image})
            call("POST", base + "/deployments", {"name": "function"})
            call(
                "POST",
                base + "/deployments/function/revisions",
                {"model": "function", "version": version["version"]},
            )
            poll(
                lambda: call("GET", base + "/endpoints/function"),
                lambda e: e["status"] == "READY",
                "function",
            )
            function_key = open_endpoint("function")
            old = invoke("function", function_key, {}, "invoke")
            if old["credential_sha256"] != hashlib.sha256(value.encode()).hexdigest():
                raise RuntimeError("initial credential mismatch")
            value = secrets.token_hex(24)
            rotated = call(
                "PUT",
                base + "/secrets/function-credential",
                {"values": {"value": value}, "expected_version": credential["version"]},
            )
            if (
                invoke("function", function_key, {}, "invoke")["credential_sha256"]
                != old["credential_sha256"]
            ):
                raise RuntimeError("running container unexpectedly changed credentials")
            kubectl(
                "-n",
                namespace,
                "delete",
                "pods",
                "-l",
                "serving.kserve.io/inferenceservice=function",
                "--wait=false",
            )
            poll(
                lambda: gateway.post(
                    f"/v1/{project}/function/invoke",
                    json={},
                    headers={"authorization": "Bearer " + function_key},
                ),
                lambda r: (
                    r.status_code == 200
                    and r.json().get("credential_sha256")
                    == hashlib.sha256(value.encode()).hexdigest()
                ),
                "credential restart",
            )
            blocked = api.delete(
                base + "/secrets/function-credential",
                params={"expected_version": rotated["version"]},
            )
            if blocked.status_code != 409:
                raise RuntimeError("referenced deletion was not protected")
            record(PHASES[4])
            before = json.loads(kubectl("-n", namespace, "get", "isvc/function", "-o", "json"))
            old_backend = before["status"]["components"]["predictor"]["latestReadyRevision"]
            old_apply = before["spec"]["predictor"]["annotations"]["mlp.io/apply-id"]
            kubectl(
                "-n",
                namespace,
                "patch",
                "isvc/function",
                "--type=json",
                "-p",
                json.dumps(
                    [
                        {
                            "op": "add",
                            "path": "/spec/predictor/containers/0/env/-",
                            "value": {"name": "DRIFT_MARKER", "value": "true"},
                        }
                    ]
                ),
            )
            repaired = poll(
                lambda: json.loads(kubectl("-n", namespace, "get", "isvc/function", "-o", "json")),
                lambda s: _repaired_backend_matches(
                    s,
                    lambda name: json.loads(
                        kubectl("-n", namespace, "get", "revision/" + name, "-o", "json")
                    ),
                    old_apply,
                    old_backend,
                ),
                "drift repair",
            )
            if (
                repaired["spec"]["predictor"]["annotations"]["mlp.io/revision"]
                != before["spec"]["predictor"]["annotations"]["mlp.io/revision"]
            ):
                raise RuntimeError("drift repair changed platform revision")
            if any(
                e["name"] == "DRIFT_MARKER"
                for e in repaired["spec"]["predictor"]["containers"][0].get("env", [])
            ):
                raise RuntimeError("owned predictor drift was not removed")
            backend = repaired["status"]["components"]["predictor"]["latestReadyRevision"]
            revision = json.loads(
                kubectl("-n", namespace, "get", "revision/" + backend, "-o", "json")
            )
            if (
                revision["metadata"]["annotations"].get("mlp.io/apply-id")
                != repaired["spec"]["predictor"]["annotations"]["mlp.io/apply-id"]
            ):
                raise RuntimeError("backend apply identity mismatch")
            poll(
                lambda: call("GET", base + "/endpoints/function"),
                lambda e: e["status"] == "READY",
                "repaired endpoint",
            )
            record(PHASES[5], previous_backend=old_backend, repaired_backend=backend)
            active = invoke("function", function_key, {}, "invoke")
            poll(
                lambda: json.loads(
                    kubectl(
                        "-n",
                        namespace,
                        "get",
                        "pods",
                        "-l",
                        "serving.kserve.io/inferenceservice=function",
                        "-o",
                        "json",
                    )
                )["items"],
                lambda pods: not pods,
                "scale to zero",
            )
            activation_started = time.monotonic()
            awake = invoke("function", function_key, {}, "invoke")
            if awake["boot_id"] == active["boot_id"]:
                raise RuntimeError("new activation instance was not observed")
            record(PHASES[6], activation_request_seconds=time.monotonic() - activation_started)
            report["passed"] = True
        finally:
            report["elapsed_seconds"] = time.monotonic() - started
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(report, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--prometheus", required=True)
    parser.add_argument(
        "--storage-secret", type=Path, help="SecretWrite JSON for S3 credentials/settings"
    )
    parser.add_argument(
        "--registry-secret", type=Path, help="SecretWrite JSON for private registry"
    )
    parser.add_argument("--training-image", required=True)
    parser.add_argument("--function-image", required=True)
    parser.add_argument("--training-env", type=Path)
    parser.add_argument("--system-namespace", default="mlp-system")
    parser.add_argument("--release", default="mlp")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    for image in (args.training_image, args.function_image):
        if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image):
            parser.error("acceptance images must be digest pinned")
    if not args.execute:
        print(json.dumps({"context": args.context, "phases": PHASES, "execute": False}, indent=2))
        return
    execute(args)


if __name__ == "__main__":
    main()
