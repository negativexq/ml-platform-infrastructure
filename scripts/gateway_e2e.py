"""End-to-end check of the inference gateway as it runs in production, minus Kubernetes.

Real PostgreSQL (migrated with Alembic), the real gateway process (`controlplane.gateway_main`
under uvicorn) and a stand-in model server speaking KServe's v2 protocol over HTTP. The
platform state (project, deployment, a public endpoint, keys) is written with the control
plane's own services, exactly as the API would.

    python scripts/gateway_e2e.py      # needs the controlplane and controlplane-dev extras

What is not covered: KServe itself, Knative's gateway, the ingress and TLS
(docs/local-verification.md §12).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STAND_IN = """
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import sys

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if self.path != "/v2/models/credit-risk-prod/infer":
            self.send_response(404); self.end_headers(); return
        out = json.dumps({"predictions": [sum(row) for row in body["instances"]],
                          "request_id": self.headers.get("x-request-id")}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)
    def log_message(self, *args):
        pass

HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for(url: str) -> None:
    for _ in range(100):
        try:
            urllib.request.urlopen(url, timeout=1)  # noqa: S310
            return
        except urllib.error.HTTPError:
            return  # it answered
        except OSError:
            time.sleep(0.2)
    raise SystemExit(f"{url} never came up")


def call(url: str, token: str | None, body: dict[str, Any]) -> tuple[int, dict[str, str], Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"content-type": "application/json"},
    )
    if token:
        request.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as r:  # noqa: S310
            return r.status, dict(r.headers), json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.load(e)


def check(what: str, ok: bool) -> None:
    print(f"  {'ok ' if ok else 'FAIL'}  {what}")
    if not ok:
        raise SystemExit(1)


def main() -> None:
    import pgserver

    from controlplane.adapters.fakes import (
        FakeClusterProvider,
        FakeExperimentProvider,
        FakeServingProvider,
    )
    from controlplane.application.api_access import ApiAccessService
    from controlplane.application.deployments import DeploymentService
    from controlplane.application.projects import CreateProject, ProjectService
    from controlplane.application.providers import RegisteredVersion
    from controlplane.domain.entities import EndpointLimits, Model, ModelVersion
    from controlplane.domain.states import Exposure, ModelStatus
    from controlplane.persistence.sql import SqlUnitOfWork, make_engine, sql_uow_factory
    from controlplane.reconciliation.deployments import DeploymentReconciler
    from controlplane.reconciliation.projects import ProjectReconciler

    pg = pgserver.get_server(  # type: ignore[attr-defined]
        tempfile.mkdtemp(prefix="gw-pg-"), cleanup_mode="stop"
    )
    db = pg.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    env = {
        **os.environ,
        "CP_DATABASE_URL": db,
        "CP_AUTH_MODE": "none",
        "CP_DATABASE_ROLE_ENFORCEMENT": "false",
        "PYTHONPATH": str(ROOT),
    }
    subprocess.run(
        [sys.executable, "-m", "controlplane.persistence.migrate", "upgrade"], env=env, check=True
    )
    print("database migrated")

    model_port, gateway_port = free_port(), free_port()
    procs = [
        subprocess.Popen([sys.executable, "-c", STAND_IN, str(model_port)]),
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "controlplane.gateway_main:app_factory",
                "--factory",
                "--port",
                str(gateway_port),
                "--log-level",
                "warning",
            ],
            env=env,
        ),
    ]
    try:
        sessions = sql_uow_factory(make_engine(db))

        def uow() -> SqlUnitOfWork:
            return SqlUnitOfWork(sessions)

        project, _ = ProjectService(uow).create(CreateProject(name="credit-risk"))
        ProjectReconciler(uow, FakeClusterProvider()).reconcile(project.id)
        experiments, serving = FakeExperimentProvider(), FakeServingProvider()
        with uow() as u:
            model = Model.create(
                project_id=project.id, name="scorer", thresholds={}, now=project.created_at
            )
            u.models.add(model)
            u.model_versions.add(
                ModelVersion(
                    model_id=model.id,
                    version=1,
                    status=ModelStatus.CANDIDATE,
                    external_ref="1",
                    created_at=project.created_at,
                    updated_at=project.created_at,
                )
            )
            u.commit()
        experiments.registered["credit-risk-scorer"] = [RegisteredVersion("1", None)]
        deployments = DeploymentService(uow, experiments=experiments, serving=serving)
        deployments.create("credit-risk", "credit-risk-prod")
        deployments.deploy("credit-risk", "credit-risk-prod", "scorer", 1)
        DeploymentReconciler(uow, serving).reconcile_all()
        with uow() as u:  # point the endpoint at the stand-in model server
            endpoint = u.endpoints.get_by_name(project.id, "credit-risk-prod")
            assert endpoint is not None and endpoint.status.value == "READY", endpoint
            from dataclasses import replace

            u.endpoints.update(
                replace(endpoint, url=f"http://127.0.0.1:{model_port}"),
                expected_status=endpoint.status,
            )
            u.commit()
        access = ApiAccessService(uow)
        access.expose(
            "credit-risk", "credit-risk-prod", Exposure.PUBLIC, EndpointLimits(units_per_minute=5)
        )
        key, token = access.create_key(
            "credit-risk", name="partner-acme", endpoints=["credit-risk-prod"]
        )
        print("platform state written: public endpoint, one key")

        base = f"http://127.0.0.1:{gateway_port}"
        wait_for(f"{base}/healthz")
        url = f"{base}/v1/credit-risk/credit-risk-prod/predict"
        print(f"calling {url}")

        status, headers, body = call(url, token, {"instances": [[1, 2], [3, 4]]})
        check("a key gets the model's answer", status == 200 and body["predictions"] == [3, 7])
        check(
            "the request id reaches the model and comes back",
            body["request_id"] == headers["x-request-id"],
        )
        check("the serving version is named", headers.get("x-mlp-model") == "scorer v1")
        check("rate-limit headers", headers.get("ratelimit-limit") == "5")
        status, _, body = call(url, None, {"instances": [[1]]})
        check(
            "no key: 401 with the error shape",
            status == 401 and body["error"]["code"] == "unauthenticated",
        )
        codes = [call(url, token, {"instances": [[1]]})[0] for _ in range(5)]
        check(f"the endpoint limit holds (5/min): {codes}", codes.count(429) >= 1)
        access.revoke_key("credit-risk", key.key_id)
        time.sleep(5.5)
        status, _, body = call(url, token, {"instances": [[1]]})
        check("a revoked key stops within the cache time", status == 401)
        with uow() as u:
            stored = u.api_keys.get(key.key_id)
        check(
            "last use was written back to the database",
            stored is not None and stored.last_used_at is not None,
        )
        print("gateway end-to-end: all checks passed")
    finally:
        for p in procs:
            p.terminate()
            p.wait(timeout=10)
        pg.cleanup()


if __name__ == "__main__":
    main()
