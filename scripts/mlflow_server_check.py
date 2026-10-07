#!/usr/bin/env python3
"""Exercise tracking/registry and S3 proxy operations in disposable Docker fixtures."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import boto3
import httpx
from mlflow import MlflowClient

from controlplane.adapters.mlflow.tracking import MlflowExperimentProvider


def run(args: list[str], log: Path | None = None, redact: tuple[str, ...] = ()) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=180)
    if log:
        diagnostic = result.stdout + result.stderr
        for value in redact:
            diagnostic = diagnostic.replace(value, "<redacted>")
        log.write_text(diagnostic)
    if result.returncode:
        raise RuntimeError("MLflow fixture operation failed; credential arguments suppressed")
    return result.stdout.strip()


def check(image: str, minio_image: str, postgres_image: str, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, object] = {"passed": False, "checks": []}
    checks: list[str] = []
    report["checks"] = checks
    prefix = "mlp-mlflow-audit-" + uuid4().hex[:10]
    containers: list[str] = []
    network_created = False

    def record(name: str) -> None:
        checks.append(name)
        print("PASS", name, flush=True)

    def port(container: str, internal: str) -> str:
        mapping = json.loads(run(["docker", "inspect", container]))[0]
        ports = mapping["NetworkSettings"]["Ports"].get(internal)
        if not ports:
            raise RuntimeError("fixture port is not available")
        host_port = ports[0]["HostPort"]
        return "http://127.0.0.1:" + host_port

    try:
        image_id = run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
        report["image_id"] = image_id
        run(["docker", "run", "--rm", "--read-only", image_id, "python", "-m", "pip", "check"])
        record("runtime pip check")
        # Docker Desktop does not publish ephemeral host ports on internal networks.
        # Both exposed fixture ports are bound only to host loopback.
        run(["docker", "network", "create", prefix])
        network_created = True
        password, s3_password = secrets.token_hex(24), secrets.token_hex(24)
        db, minio, server = prefix + "-db", prefix + "-s3", prefix + "-server"
        containers.append(db)
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                db,
                "--network",
                prefix,
                "-e",
                "POSTGRES_USER=platform",
                "-e",
                "POSTGRES_DB=mlflow",
                "-e",
                "POSTGRES_PASSWORD=" + password,
                postgres_image,
            ]
        )
        containers.append(minio)
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                minio,
                "--network",
                prefix,
                "-p",
                "127.0.0.1::9000",
                "--tmpfs",
                "/data:rw,nosuid,uid=1000,gid=1000",
                "-e",
                "MINIO_ROOT_USER=audit",
                "-e",
                "MINIO_ROOT_PASSWORD=" + s3_password,
                minio_image,
                "server",
                "/data",
            ]
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                run(["docker", "exec", db, "pg_isready", "-U", "platform", "-d", "mlflow"])
                if httpx.get(port(minio, "9000/tcp") + "/minio/health/ready").status_code == 200:
                    break
            except (RuntimeError, httpx.HTTPError):
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("fixture readiness timeout")
        s3 = boto3.client(
            "s3",
            endpoint_url=port(minio, "9000/tcp"),
            aws_access_key_id="audit",
            aws_secret_access_key=s3_password,
            region_name="us-east-1",
        )
        s3.create_bucket(Bucket="audit")
        uri = f"postgresql+psycopg2://platform:{password}@{db}:5432/mlflow"
        hardening = [
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,uid=1000",
            "--tmpfs",
            "/home/mlflow:rw,noexec,nosuid,uid=1000",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--network",
            prefix,
        ]
        run(
            ["docker", "run", "--rm", *hardening, image_id, "mlflow", "db", "upgrade", uri],
            log=output / "db-upgrade.log",
            redact=(password, s3_password),
        )
        record("PostgreSQL database upgrade")
        containers.append(server)
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                server,
                *hardening,
                "-p",
                "127.0.0.1::5000",
                "-e",
                "AWS_ACCESS_KEY_ID=audit",
                "-e",
                "AWS_SECRET_ACCESS_KEY=" + s3_password,
                "-e",
                "AWS_DEFAULT_REGION=us-east-1",
                "-e",
                f"MLFLOW_S3_ENDPOINT_URL=http://{minio}:9000",
                image_id,
                "mlflow",
                "server",
                "--host=0.0.0.0",
                "--port=5000",
                "--workers=1",
                "--backend-store-uri=" + uri,
                "--default-artifact-root=s3://audit/",
                "--artifacts-destination=s3://audit/proxy/",
                "--allowed-hosts=127.0.0.1:*",
            ]
        )
        url = port(server, "5000/tcp")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                if httpx.get(url + "/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("MLflow server readiness timeout")
        record("server health")
        ui = httpx.get(url + "/")
        assert ui.status_code == 200 and "text/html" in ui.headers.get("content-type", "")
        record("upstream UI index")
        client = MlflowClient(tracking_uri=url)
        provider = MlflowExperimentProvider(url)
        experiment = provider.ensure_experiment(uuid4(), "audit")
        assert client.get_experiment(experiment).name == "audit"
        run_id = client.create_run(experiment, tags={"audit": "yes"}).info.run_id
        client.log_param(run_id, "depth", "2")
        client.log_metric(run_id, "r2", 0.98)
        client.set_terminated(run_id)
        assert provider.get_run(run_id).metrics["r2"] == 0.98
        assert len(provider.find_runs(experiment, {"audit": "yes"})) == 1
        record("experiment create/read and run logging/search")
        logged = client.create_logged_model(experiment, name="audit-model", source_run_id=run_id)
        client.finalize_logged_model(logged.model_id, "READY")
        assert client.get_logged_model(logged.model_id).artifact_location.startswith("s3://audit/")
        client.create_registered_model("audit-model")
        version = client.create_model_version(
            "audit-model", "models:/" + logged.model_id, run_id, model_id=logged.model_id
        )
        assert (
            provider.model_artifact_uri("audit-model", str(version.version))
            == logged.artifact_location
        )
        assert len(provider.list_model_versions("audit-model")) == 1
        provider.set_model_alias("audit-model", "champion", str(version.version))
        assert provider.get_model_alias("audit-model", "champion") == str(version.version)
        provider.delete_model_alias("audit-model", "champion")
        assert provider.get_model_alias("audit-model", "champion") is None
        record("logged-model lookup, registry, aliases and control-plane discovery")
        proxy_experiment = client.create_experiment(
            "proxy", artifact_location="mlflow-artifacts:/audit"
        )
        proxy_run = client.create_run(proxy_experiment).info.run_id
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "proof.txt"
            artifact.write_text("MLflow S3 round trip\n")
            client.log_artifact(proxy_run, str(artifact))
            assert client.list_artifacts(proxy_run)[0].path == "proof.txt"
            downloaded = client.download_artifacts(
                proxy_run, "proof.txt", str(Path(directory) / "out")
            )
            assert Path(downloaded).read_bytes() == artifact.read_bytes()
        assert s3.list_objects_v2(Bucket="audit", Prefix="proxy/").get("KeyCount", 0) > 0
        record("server-proxied S3 artifact upload/list/download")
        report["passed"] = True
    finally:
        for container in containers:
            logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
            # Fixture credentials could occur in upstream startup logs; redact before archiving.
            text = logs.stdout + logs.stderr
            for value in (locals().get("password", ""), locals().get("s3_password", "")):
                if value:
                    text = text.replace(value, "<redacted>")
            (output / (container.rsplit("-", 1)[1] + ".log")).write_text(text)
            subprocess.run(["docker", "rm", "-f", "-v", container], capture_output=True)
        if network_created:
            subprocess.run(["docker", "network", "rm", prefix], capture_output=True)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--minio-image", required=True)
    parser.add_argument("--postgres-image", default="postgres:17-bookworm")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    # Suppress SDK progress output; fixture credentials remain in subprocess arguments only.
    os.environ["MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR"] = "false"
    # The fixture's MinIO DNS name is private to Docker; test server-proxied downloads.
    os.environ["MLFLOW_ENABLE_PROXY_MULTIPART_DOWNLOAD"] = "false"
    os.environ["MLFLOW_HTTP_REQUEST_TIMEOUT"] = "15"
    os.environ["MLFLOW_HTTP_REQUEST_MAX_RETRIES"] = "0"
    check(args.image, args.minio_image, args.postgres_image, args.out)


if __name__ == "__main__":
    main()
