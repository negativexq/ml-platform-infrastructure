#!/usr/bin/env python3
"""Manual image gate. Starts isolated Docker fixtures; never installs into a cluster."""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def run(args: list[str], *, timeout: int = 600) -> str:
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        # Commands can contain ephemeral credentials; do not echo them or raw output.
        raise RuntimeError(f"{args[0]} operation failed (exit {result.returncode})")
    return result.stdout.strip()


def check(image: str, postgres_image: str, output: Path, build: bool) -> None:
    if output.exists():
        raise ValueError("report directory must not already exist")
    output.mkdir(parents=True, mode=0o700)
    prefix = "mlp-release-" + uuid4().hex[:10]
    containers: list[str] = []
    network = prefix
    network_created = False
    report: dict[str, Any] = {"started_at": time.time(), "passed": False, "checks": []}

    def record(name: str) -> None:
        report["checks"].append(name)
        print("PASS", name, flush=True)

    try:
        missing = [tool for tool in ("docker", "trivy", "syft") if not shutil.which(tool)]
        if missing:
            raise RuntimeError("release tools missing: " + ", ".join(missing))
        report["source_revision"] = run(["git", "-C", str(ROOT), "rev-parse", "HEAD"])
        report["source_clean"] = not run(["git", "-C", str(ROOT), "status", "--porcelain"])
        if not report["source_clean"]:
            raise RuntimeError("release check requires a clean committed source tree")
        if build:
            run(
                [
                    "docker",
                    "build",
                    "-f",
                    str(ROOT / "docker/controlplane/Dockerfile"),
                    "--build-arg",
                    "SOURCE_REVISION=" + report["source_revision"],
                    "-t",
                    image,
                    str(ROOT),
                ],
                timeout=1800,
            )
        image_id = run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
        report["image_id"] = image_id
        label = run(
            [
                "docker",
                "image",
                "inspect",
                "--format",
                '{{index .Config.Labels "org.opencontainers.image.revision"}}',
                image_id,
            ]
        )
        if label != report["source_revision"]:
            raise RuntimeError("image source revision does not match the clean checkout")
        run(["docker", "network", "create", "--internal", network])
        network_created = True
        password = secrets.token_hex(24)
        database = prefix + "-db"
        containers.append(database)
        run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                database,
                "--network",
                network,
                "--network-alias",
                "db",
                "-e",
                "POSTGRES_DB=controlplane",
                "-e",
                "POSTGRES_USER=platform",
                "-e",
                "POSTGRES_PASSWORD=" + password,
                postgres_image,
            ]
        )
        report["postgres_image_id"] = run(["docker", "inspect", "--format", "{{.Image}}", database])
        deadline = time.monotonic() + 90
        while True:
            try:
                run(
                    [
                        "docker",
                        "exec",
                        database,
                        "pg_isready",
                        "-U",
                        "platform",
                        "-d",
                        "controlplane",
                    ],
                    timeout=5,
                )
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("PostgreSQL startup deadline exceeded") from None
                time.sleep(0.5)
        url = f"postgresql+psycopg://platform:{password}@db:5432/controlplane"
        with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
            kubeconfig = Path(temporary) / "config"
            kubeconfig.write_text("""apiVersion: v1
kind: Config
clusters:
- name: smoke
  cluster: {server: 'http://127.0.0.1:9'}
users:
- name: smoke
  user: {}
contexts:
- name: smoke
  context: {cluster: smoke, user: smoke}
current-context: smoke
""")
            kubeconfig.chmod(0o644)  # Contains no credentials; image runs as uid 10001.
            common = [
                "--label",
                "mlp.release-check=" + prefix,
                "--network",
                network,
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=64m",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "-e",
                "CP_AUTH_MODE=none",
                "-e",
                "CP_DATABASE_ROLE_ENFORCEMENT=false",
                "-e",
                "CP_DATABASE_URL=" + url,
                "-e",
                "CP_KUBECONFIG=/fixtures/config",
                "-e",
                "CP_LEADER_ELECTION_ENABLED=false",
                "-e",
                "CP_RECONCILE_INTERVAL_SECONDS=1",
                "-v",
                str(kubeconfig) + ":/fixtures/config:ro",
            ]
            run(
                [
                    "docker",
                    "run",
                    "--rm",
                    *common,
                    image_id,
                    "python",
                    "-m",
                    "controlplane.persistence.migrate",
                    "upgrade",
                ]
            )
            head = run(
                [
                    "docker",
                    "exec",
                    database,
                    "psql",
                    "-U",
                    "platform",
                    "-d",
                    "controlplane",
                    "-Atc",
                    "SELECT version_num FROM alembic_version",
                ]
            )
            expected_head = run(
                [
                    "docker",
                    "run",
                    "--rm",
                    *common,
                    image_id,
                    "python",
                    "-c",
                    "from alembic.script import ScriptDirectory; "
                    "from controlplane.persistence.migrate import _config; "
                    "print(','.join(ScriptDirectory.from_config(_config(None)).get_heads()))",
                ]
            )
            if head != expected_head:
                raise RuntimeError("image schema head mismatch")
            record("migration clean database to " + head)
            for component, port in (("api", 8080), ("gateway", 8081)):
                name = prefix + "-" + component
                containers.append(name)
                run(
                    [
                        "docker",
                        "run",
                        "-d",
                        "--name",
                        name,
                        "--network-alias",
                        component,
                        *common,
                        image_id,
                        "uvicorn",
                        "controlplane.gateway_main:app_factory"
                        if component == "gateway"
                        else "controlplane.main:app_factory",
                        "--factory",
                        "--host=0.0.0.0",
                        f"--port={port}",
                    ]
                )
            probe = """import sys, urllib.request, urllib.error
try:
 r=urllib.request.urlopen(sys.argv[1], timeout=8); code=r.status
except urllib.error.HTTPError as e: code=e.code
assert code==int(sys.argv[2]), (code,sys.argv[2])
"""

            def http(component: str, port: int, path: str, status: int) -> None:
                run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        *common,
                        image_id,
                        "python",
                        "-c",
                        probe,
                        f"http://{component}:{port}/{path}",
                        str(status),
                    ],
                    timeout=15,
                )

            for component, port in (("api", 8080), ("gateway", 8081)):
                deadline = time.monotonic() + 60
                while True:
                    try:
                        http(component, port, "readyz", 200)
                        http(component, port, "healthz", 200)
                        break
                    except RuntimeError:
                        if time.monotonic() > deadline:
                            raise RuntimeError(component + " startup deadline exceeded") from None
                        time.sleep(0.5)
                record(component + " health/readiness")
            name = prefix + "-reconciler"
            containers.append(name)
            run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    name,
                    *common,
                    image_id,
                    "python",
                    "-m",
                    "controlplane.reconciler_main",
                ]
            )
            deadline = time.monotonic() + 60
            while True:
                if run(["docker", "inspect", "--format", "{{.State.Running}}", name]) != "true":
                    raise RuntimeError("reconciler exited")
                logs = run(["docker", "logs", name])
                if "pass failed" in logs:
                    raise RuntimeError("reconciler initialization/pass failed")
                if "reconciler started" in logs:
                    # Give the first empty-state pass time to finish before accepting startup.
                    time.sleep(3)
                    if run(["docker", "inspect", "--format", "{{.State.Running}}", name]) != "true":
                        raise RuntimeError("reconciler exited")
                    if "pass failed" in run(["docker", "logs", name]):
                        raise RuntimeError("reconciler initialization/pass failed")
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("reconciler startup deadline exceeded")
                time.sleep(0.5)
            record("reconciler configuration and empty-database passes (no live Kubernetes)")
            run(["docker", "stop", "--time", "10", database])
            for component, port in (("api", 8080), ("gateway", 8081)):
                http(component, port, "healthz", 200)
                http(component, port, "readyz", 503)
            record("DB outage keeps liveness, removes readiness")
            run(["docker", "start", database])
            deadline = time.monotonic() + 60
            while True:
                try:
                    http("gateway", 8081, "readyz", 200)
                    http("api", 8080, "readyz", 200)
                    break
                except RuntimeError:
                    if time.monotonic() > deadline:
                        raise RuntimeError("database recovery deadline exceeded") from None
                    time.sleep(0.5)
            record("readiness recovers after DB restart")
        archive = output / "image.tar"
        run(["docker", "save", "-o", str(archive), image_id])
        run(
            [
                "syft",
                "docker-archive:" + str(archive),
                "-o",
                "spdx-json=" + str(output / "sbom.spdx.json"),
            ],
            timeout=600,
        )
        record("SPDX SBOM")
        run(
            [
                "trivy",
                "image",
                "--input",
                str(archive),
                "--scanners",
                "vuln",
                "--format",
                "json",
                "--output",
                str(output / "trivy.json"),
                "--severity",
                "HIGH,CRITICAL",
                "--ignore-unfixed",
                "--exit-code",
                "1",
            ],
            timeout=1800,
        )
        record("Trivy fixable HIGH/CRITICAL gate")
        run(
            [
                "trivy",
                "image",
                "--input",
                str(archive),
                "--scanners",
                "secret",
                "--format",
                "json",
                "--output",
                str(output / "trivy-secrets.json"),
                "--exit-code",
                "1",
            ],
            timeout=1800,
        )
        record("Trivy secrets gate (all severities)")
        archive.unlink()
        report["passed"] = True
    finally:
        for name in reversed(containers):
            subprocess.run(
                ["docker", "rm", "-f", "-v", name], capture_output=True, timeout=30, check=False
            )
        if network_created:
            remaining = subprocess.run(
                ["docker", "ps", "-aq", "--filter", "label=mlp.release-check=" + prefix],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if remaining.returncode == 0:
                for container_id in remaining.stdout.split():
                    subprocess.run(
                        ["docker", "rm", "-f", "-v", container_id],
                        capture_output=True,
                        timeout=30,
                        check=False,
                    )
        if network_created:
            subprocess.run(
                ["docker", "network", "rm", network], capture_output=True, timeout=30, check=False
            )
        report["finished_at"] = time.time()
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="mlp-controlplane:release-check")
    parser.add_argument("--postgres-image", default="postgres:17-bookworm")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    check(args.image, args.postgres_image, args.out, args.build)


if __name__ == "__main__":
    main()
