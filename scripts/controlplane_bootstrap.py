#!/usr/bin/env python3
"""Prepare a pinned, verified installation bundle. Default operation never contacts a cluster."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "scripts/controlplane-dependencies.json"


def command(args: list[str]) -> str:
    return subprocess.check_output(args, text=True)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mlflow_runtime(image: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("MLflow serving image must be pinned with @sha256:<64 lowercase digits>")
    return {
        "apiVersion": "serving.kserve.io/v1alpha1",
        "kind": "ClusterServingRuntime",
        "metadata": {"name": "mlp-mlflow"},
        "spec": {
            "protocolVersions": ["v2"],
            "supportedModelFormats": [
                {"name": "mlflow", "version": version, "autoSelect": True, "priority": 10}
                for version in ("1", "2", "3")
            ],
            "containers": [
                {
                    "name": "kserve-container",
                    "image": image,
                    "env": [
                        {"name": "MLSERVER_MODEL_NAME", "value": "{{.Name}}"},
                        {"name": "MLSERVER_MODEL_URI", "value": "/mnt/models"},
                        {
                            "name": "MLSERVER_MODEL_IMPLEMENTATION",
                            "value": "mlserver_mlflow.MLflowRuntime",
                        },
                    ],
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "256Mi"},
                        "limits": {"cpu": "1", "memory": "1Gi"},
                    },
                    "securityContext": {
                        "runAsUser": 1000,
                        "runAsNonRoot": True,
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                }
            ],
        },
    }


def s3_storage_initializer(image: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("S3 initializer image must be digest-pinned")
    runtime = mlflow_runtime(image)
    container = runtime["spec"]["containers"][0]
    return {
        "apiVersion": "serving.kserve.io/v1alpha1",
        "kind": "ClusterStorageContainer",
        "metadata": {"name": "mlp-s3"},
        "spec": {
            "workloadType": "initContainer",
            "supportedUriFormats": [{"prefix": "s3://"}],
            "container": {
                "name": "storage-initializer",
                "image": image,
                "securityContext": container["securityContext"],
                "resources": container["resources"],
            },
        },
    }


def dependencies(destination: Path, fetch: bool) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for artifact in json.loads(LOCK.read_text())["artifacts"]:
        target = destination / artifact["file"]
        if not target.exists() and fetch:
            if "url" in artifact:
                with urllib.request.urlopen(artifact["url"], timeout=30) as response:
                    target.write_bytes(response.read())
            else:
                with tempfile.TemporaryDirectory(prefix="mlp-chart-") as staging:
                    command(
                        [
                            "helm",
                            "pull",
                            artifact["oci"] + "@" + artifact["oci_digest"],
                            "--destination",
                            staging,
                        ]
                    )
                    charts = list(Path(staging).glob("*.tgz"))
                    if len(charts) != 1:
                        raise RuntimeError("unexpected chart download")
                    target.write_bytes(charts[0].read_bytes())
        if target.exists() and sha256(target) != artifact["sha256"]:
            raise RuntimeError("dependency checksum mismatch: " + artifact["name"])


def prepare(
    output: Path,
    site_values: Path,
    image: str,
    revision: str,
    context: str,
    fetch: bool = False,
    cache: Path | None = None,
    gateway_service_type: str = "LoadBalancer",
    mlflow_serving_image: str | None = None,
    s3_storage_initializer_image: str | None = None,
    migration_image: str | None = None,
) -> Path:
    runtime = mlflow_runtime(mlflow_serving_image) if mlflow_serving_image else None
    initializer = (
        s3_storage_initializer(s3_storage_initializer_image)
        if s3_storage_initializer_image
        else None
    )
    if gateway_service_type not in {"LoadBalancer", "ClusterIP"}:
        raise ValueError("gateway service type must be LoadBalancer or ClusterIP")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("source revision must be a full 40-character Git commit SHA")
    if not re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("control-plane image must be pinned with @sha256:<64 lowercase digits>")
    values = yaml.safe_load(site_values.read_text()) or {}
    if not isinstance(values, dict):
        raise ValueError("site values must be a mapping")
    if migration_image:
        if not re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[0-9a-f]{64}", migration_image):
            raise ValueError("migration image must be digest-pinned")
        values["migrations"] = {
            **values.get("migrations", {}),
            "image": {
                **values.get("migrations", {}).get("image", {}),
                "repository": migration_image.split("@")[0],
                "digest": migration_image.split("@")[1],
                "tag": "",
            },
        }
    values = {
        **values,
        "image": {
            **values.get("image", {}),
            "repository": image.split("@")[0],
            "digest": image.split("@")[1],
            "tag": "",
        },
    }
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    deps = cache or output / "dependencies"
    dependencies(deps, fetch)
    values_path = output / "values.yaml"
    values_path.write_text(yaml.safe_dump(values, sort_keys=False))
    lock_copy = output / "dependency-lock.json"
    lock_copy.write_bytes(LOCK.read_bytes())
    command(["helm", "package", str(ROOT / "helm/controlplane"), "--destination", str(output)])
    chart_version = yaml.safe_load((ROOT / "helm/controlplane/Chart.yaml").read_text())["version"]
    chart_archive = output / f"controlplane-{chart_version}.tgz"
    try:
        source_verified = (
            command(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).strip() == revision
            and not command(["git", "-C", str(ROOT), "status", "--porcelain"]).strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        source_verified = False
    rendered = command(
        [
            "helm",
            "template",
            "mlp",
            str(chart_archive),
            "--namespace",
            "mlp-system",
            "--values",
            str(values_path),
        ]
    )
    (output / "controlplane.yaml").write_text(rendered)
    application: dict[str, Any] = {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {"name": "mlp-controlplane", "namespace": "argocd"},
        "spec": {
            "project": "default",
            "source": {
                "repoURL": "https://github.com/negativexq/ml-platform-infrastructure.git",
                "targetRevision": revision,
                "path": "helm/controlplane",
                "helm": {"releaseName": "mlp", "values": yaml.safe_dump(values)},
            },
            "destination": {"server": "https://kubernetes.default.svc", "namespace": "mlp-system"},
            "syncPolicy": {"syncOptions": ["CreateNamespace=true"]},
        },
    }
    # Manual sync: generating this draft does not install or change an Argo CD Application.
    (output / "application.yaml").write_text(yaml.safe_dump(application, sort_keys=False))
    serving = {
        "apiVersion": "operator.knative.dev/v1beta1",
        "kind": "KnativeServing",
        "metadata": {"name": "knative-serving", "namespace": "knative-serving"},
        "spec": {
            "version": json.loads(LOCK.read_text())["knative_serving_version"],
            "config": {
                "features": {
                    "kubernetes.podspec-securitycontext": "enabled",
                    "kubernetes.podspec-init-containers": "enabled",
                    "secure-pod-defaults": "enabled",
                }
            },
        },
    }
    serving_path = output / "knative-serving.yaml"
    serving_path.write_text(yaml.safe_dump(serving, sort_keys=False))
    runtime_path = output / "mlflow-runtime.yaml"
    if runtime:
        runtime_path.write_text(yaml.safe_dump(runtime, sort_keys=False))
    initializer_path = output / "s3-storage-initializer.yaml"
    initializer_patch_path = output / "default-storage-formats.json"
    if initializer:
        initializer_path.write_text(yaml.safe_dump(initializer, sort_keys=False))
        # Keep the pinned KServe default for other providers; each S3 URI must match
        # only our container, regardless of Kubernetes list ordering.
        initializer_patch_path.write_text(
            json.dumps(
                {
                    "spec": {
                        "supportedUriFormats": [
                            {"prefix": prefix}
                            for prefix in ("gs://", "hdfs://", "hf://", "webhdfs://")
                        ]
                        + [
                            {"regex": r"https://(.+?).blob.core.windows.net/(.+)"},
                            {"regex": r"https://(.+?).file.core.windows.net/(.+)"},
                            {"regex": r"https?://(.+)/(.+)"},
                        ]
                    }
                }
            )
            + "\n"
        )
    q = shlex.quote
    kubectl = "kubectl --context " + q(context)
    helm = "helm --kube-context " + q(context)
    steps = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        "# Review the bundle; this installer changes the named cluster.",
        "# Istio 1.23's defaults schema requires the verified Helm 3.17 renderer.",
        'case "$(helm version --short)" in',
        "  v3.17.*) ;;",
        "  *) echo 'Pinned dependencies require Helm 3.17.x (verified: 3.17.3). "
        "Select it through PATH before running this installer.' >&2; exit 1 ;;",
        "esac",
    ]
    if not source_verified:
        steps += [
            "echo 'Review-only bundle: regenerate from a clean checkout "
            "of the specified source commit.' >&2",
            "exit 1",
        ]
    if context == "REPLACE_WITH_CLUSTER_CONTEXT":
        steps += [
            "echo 'Regenerate with an explicit cluster context before installation.' >&2",
            "exit 1",
        ]
    artifacts = json.loads(lock_copy.read_text())["artifacts"]
    steps += [
        "python3 - "
        + q(str(lock_copy))
        + " "
        + q(str(deps.resolve()))
        + " "
        + q(str(chart_archive))
        + " "
        + q(sha256(chart_archive))
        + " <<'PY'",
        "import hashlib,json,sys,pathlib",
        "if hashlib.sha256(pathlib.Path(sys.argv[3]).read_bytes()).hexdigest()!=sys.argv[4]:",
        "    raise SystemExit('control-plane chart checksum mismatch')",
        "for artifact in json.load(open(sys.argv[1]))['artifacts']:",
        "    path=pathlib.Path(sys.argv[2])/artifact['file']",
        "    if hashlib.sha256(path.read_bytes()).hexdigest()!=artifact['sha256']:",
        "        raise SystemExit('dependency checksum mismatch: '+artifact['name'])",
        "PY",
        f"{kubectl} get namespace mlp-system >/dev/null",
        f"{kubectl} create namespace argo --dry-run=client -o yaml | {kubectl} apply -f -",
    ]
    for component in ("api", "gateway", "reconciler", "migration"):
        secret = (
            values.get("database", {})
            .get(component, {})
            .get("existingSecret", f"mlp-controlplane-db-{component}")
        )
        steps.insert(-1, f"{kubectl} -n mlp-system get secret " + q(secret) + " >/dev/null")
    for artifact in artifacts:
        path = q(str((deps / artifact["file"]).resolve()))
        name = artifact["name"]
        if name == "gateway-api" or name == "cert-manager":
            steps.append(f"{kubectl} apply -f {path}")
            if name == "cert-manager":
                steps.append(
                    f"{kubectl} -n cert-manager rollout status "
                    "deployment/cert-manager-webhook --timeout=300s"
                )
        elif name == "argo-workflows":
            steps.append(f"{kubectl} -n argo apply -f {path}")
            steps.append(
                f"{kubectl} -n argo rollout status deployment/workflow-controller --timeout=300s"
            )
        else:
            namespace = (
                "istio-system"
                if name.startswith("istio")
                else "knative-serving"
                if name == "knative-operator"
                else "kserve"
            )
            extra = (
                " --set proxy.autoInject=disabled"
                if name == "istiod"
                else " --set-string kserve.controller.deploymentMode=Serverless"
                if name == "kserve"
                else ""
            )
            if name == "istio-ingressgateway":
                extra = " --set service.type=" + gateway_service_type
            steps.append(
                f"{helm} upgrade --install {q(name)} {path} --namespace {namespace} "
                f"--create-namespace --wait --timeout 10m{extra}"
            )
            if name == "knative-operator":
                steps.append(f"{kubectl} apply -f {q(str(serving_path.resolve()))}")
                steps.append(
                    f"{kubectl} -n knative-serving wait knativeserving/knative-serving "
                    "--for=condition=Ready --timeout=600s"
                )
    if runtime:
        steps.append(f"{kubectl} apply -f {q(str(runtime_path.resolve()))}")
    if initializer:
        steps.append(f"{kubectl} apply -f {q(str(initializer_path.resolve()))}")
        steps.append(
            f"{kubectl} patch clusterstoragecontainer default --type merge "
            f"--patch-file {q(str(initializer_patch_path.resolve()))}"
        )
    steps.append(
        f"{helm} upgrade --install mlp {q(str(chart_archive))} "
        f"--namespace mlp-system --values {q(str(values_path.resolve()))} --wait --timeout 10m"
    )
    script = output / "install.sh"
    script.write_text("\n".join(steps) + "\n")
    script.chmod(0o700)
    (output / "plan.json").write_text(
        json.dumps(
            {
                "image": image,
                "source_revision": revision,
                "context": context,
                "gateway_service_type": gateway_service_type,
                "mlflow_serving_image": mlflow_serving_image,
                "s3_storage_initializer_image": s3_storage_initializer_image,
                "migration_image": migration_image,
                "dependency_lock_sha256": sha256(lock_copy),
                "controlplane_chart_sha256": sha256(chart_archive),
                "source_verified": source_verified,
                "artifacts_cached": all((deps / a["file"]).exists() for a in artifacts),
            },
            indent=2,
        )
        + "\n"
    )
    return script


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--values", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--migration-image", required=True, help="digest-pinned migration image")
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--context", default="REPLACE_WITH_CLUSTER_CONTEXT")
    parser.add_argument("--cache", type=Path)
    parser.add_argument(
        "--mlflow-serving-image",
        help="digest-pinned MLflow runtime image built for the target node architecture",
    )
    parser.add_argument(
        "--s3-storage-initializer-image", help="digest-pinned classic S3 download image"
    )
    parser.add_argument(
        "--gateway-service-type",
        choices=("LoadBalancer", "ClusterIP"),
        default="LoadBalancer",
        help="use ClusterIP for kind without a LoadBalancer controller",
    )
    parser.add_argument(
        "--fetch", action="store_true", help="download and checksum pinned artifacts"
    )
    parser.add_argument(
        "--apply", action="store_true", help="run the reviewed cluster installation"
    )
    args = parser.parse_args()
    if args.apply and args.context == "REPLACE_WITH_CLUSTER_CONTEXT":
        parser.error("--apply requires an explicit --context")
    script = prepare(
        args.out,
        args.values,
        args.image,
        args.source_revision,
        args.context,
        args.fetch,
        args.cache,
        args.gateway_service_type,
        args.mlflow_serving_image,
        args.s3_storage_initializer_image,
        args.migration_image,
    )
    print("Prepared installation bundle:", args.out)
    if args.apply:
        if not json.loads((args.out / "plan.json").read_text())["source_verified"]:
            parser.error(
                "--apply requires a clean checkout of --source-revision; commit/push and regenerate"
            )
        subprocess.run(["bash", str(script)], check=True)


if __name__ == "__main__":
    main()
