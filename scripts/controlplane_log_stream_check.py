#!/usr/bin/env python3
"""Live Go log gate using platform-created Argo jobs and pipeline steps."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import httpx


def execute(args: argparse.Namespace) -> None:
    if args.out.exists():
        raise ValueError("report must not already exist")
    token = os.environ.get("CP_ACCEPTANCE_API_TOKEN")
    if not token:
        raise ValueError("CP_ACCEPTANCE_API_TOKEN required")
    project = "logcheck-" + uuid4().hex[:10]
    report: dict = {"passed": False, "project": project, "checks": []}
    jobs: list[tuple[str, str]] = []
    kube = ["kubectl", "--kubeconfig", args.kubeconfig]

    def record(name: str) -> None:
        report["checks"].append(name)
        print("PASS", name, flush=True)

    with httpx.Client(
        base_url=args.api, headers={"Authorization": "Bearer " + token}, timeout=20
    ) as api:

        def call(method: str, path: str, body=None):
            response = api.request(method, path, json=body)
            if response.status_code >= 400:
                raise RuntimeError(f"API {method} {path}: {response.status_code}")
            return response.json()

        def wait(read, ready, timeout=150):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                value = read()
                if ready(value):
                    return value
                time.sleep(1)
            raise RuntimeError("readiness deadline exceeded")

        try:
            created = call("POST", "/projects", {"name": project})
            wait(
                lambda: call("GET", "/projects/" + created["id"]),
                lambda p: p["status"] == "READY",
            )
            call(
                "POST",
                f"/projects/{project}/jobs",
                {
                    "name": "stream-check",
                    "image": args.training_image,
                    "command": [
                        "python",
                        "-u",
                        "-c",
                        "import time\nfor i in range(120):\n"
                        " print('stream-line-%d' % i, flush=True)\n time.sleep(1)",
                    ],
                    "resources": {"cpu": "100m", "memory": "128Mi"},
                    "timeout_seconds": 180,
                },
            )
            job = call("POST", f"/projects/{project}/jobs/stream-check/runs", {})
            jobs.append(("runs", job["id"]))
            call(
                "POST",
                f"/projects/{project}/pipelines",
                {
                    "name": "stream-pipeline",
                    "steps": [{"name": "emit", "job": "stream-check", "depends_on": []}],
                },
            )
            pipeline = call("POST", f"/projects/{project}/pipelines/stream-pipeline/runs", {})
            jobs.append(("pipeline-runs", pipeline["id"]))
            report["job_run"] = job["id"]
            report["pipeline_run"] = pipeline["id"]
            for kind, run_id in jobs:
                wait(
                    lambda kind=kind, run_id=run_id: call("GET", f"/{kind}/{run_id}"),
                    lambda run: run["status"] == "RUNNING",
                )
            record("platform-created job and pipeline are running")

            namespace = "mlp-" + project
            subject = "system:serviceaccount:" + args.system_namespace + ":" + args.service_account
            for resource, ns, expected in [
                ("pods", namespace, True),
                ("pods/log", namespace, True),
                ("secrets", namespace, False),
                ("pods", "default", False),
                ("pods/log", "kube-system", False),
            ]:
                result = subprocess.run(
                    kube + ["auth", "can-i", "get", resource, "-n", ns, "--as", subject],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if (result.stdout.strip() == "yes") != expected:
                    raise RuntimeError("unexpected log ServiceAccount permission")
            record("dedicated namespace log RBAC; no Secret or foreign namespace access")

            def ticket(path):
                return call("POST", path + "/stream-ticket", {})

            for path in [
                f"/runs/{job['id']}/logs",
                f"/pipeline-runs/{pipeline['id']}/steps/emit/logs",
            ]:
                wait(
                    lambda path=path: api.get(path).text,
                    lambda text: "stream-line-" in text,
                )
                capability = ticket(path)
                started = time.monotonic()
                with httpx.stream(
                    "GET",
                    args.stream + "/log-stream",
                    headers={"Authorization": "Bearer " + capability["token"]},
                    timeout=15,
                ) as response:
                    if response.status_code != 200 or not response.headers.get(
                        "content-type", ""
                    ).startswith("text/event-stream"):
                        raise RuntimeError(f"SSE unavailable (HTTP {response.status_code})")
                    received = False
                    for line in response.iter_lines():
                        if line.startswith("data: ") and "stream-line-" in line:
                            received = True
                            break
                    if not received or time.monotonic() - started > 15:
                        raise RuntimeError("did not receive logs before workload EOF")
                record("live SSE before EOF: " + path.split("/")[1])
                replay = httpx.get(
                    args.stream + "/log-stream",
                    headers={"Authorization": "Bearer " + capability["token"]},
                    timeout=10,
                )
                if replay.status_code != 429:
                    raise RuntimeError("capability replay accepted on same replica")
            record("single-replica replay rejection")
            capability = ticket(f"/runs/{job['id']}/logs")
            invalid = httpx.get(
                args.stream + "/log-stream",
                headers={"Authorization": "Bearer " + capability["token"] + "x"},
                timeout=10,
            )
            if invalid.status_code != 401:
                raise RuntimeError("tampered capability accepted")
            query = httpx.get(args.stream + "/log-stream?token=x", timeout=10)
            if query.status_code != 400:
                raise RuntimeError("URL credential accepted")
            record("tampered ticket and query credential rejection")

            def idle():
                text = httpx.get(args.stream + "/metrics", timeout=10).text
                return "mlp_log_stream_active 0\n" in text

            wait(idle, bool, timeout=15)
            record("disconnect releases stream admission slots")

            if args.browser:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(
                        executable_path=args.chromium, headless=True
                    )
                    try:
                        page = browser.new_page()
                        errors: list[str] = []
                        streams: list[int] = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on(
                            "response",
                            lambda response: (
                                streams.append(response.status)
                                if response.url.endswith("/log-stream")
                                else None
                            ),
                        )
                        for route in [f"runs/{job['id']}", f"pipeline-runs/{pipeline['id']}"]:
                            before = len(streams)
                            page.goto(args.browser + f"/ui/#/projects/{project}/" + route)
                            page.get_by_test_id("logs").filter(has_text="stream-line-").wait_for(
                                timeout=20000
                            )
                            if 200 not in streams[before:]:
                                raise RuntimeError("browser did not use Go SSE")
                            page.goto(args.browser + "/ui/")
                            wait(idle, bool, timeout=15)
                        if errors:
                            raise RuntimeError("browser runtime errors")
                        report["browser_stream_responses"] = streams
                    finally:
                        browser.close()
                record("browser job and pipeline SSE; navigation cancellation; no runtime errors")
            report["passed"] = True
        finally:
            for kind, run_id in jobs:
                with suppress(Exception):
                    call("POST", f"/{kind}/{run_id}/cancel", {})
            with suppress(Exception):
                projects = call("GET", "/projects")["items"]
                owned = next(p for p in projects if p["name"] == project)
                call("DELETE", "/projects/" + owned["id"])
                report["test_project_cleanup_requested"] = True
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(report, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Create live test workloads")
    parser.add_argument("--api", default="http://127.0.0.1:18906")
    parser.add_argument("--stream", default="http://127.0.0.1:18909")
    parser.add_argument("--browser", help="Same-origin proxy URL for UI and /log-stream")
    parser.add_argument(
        "--chromium", default="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--system-namespace", default="mlp-system")
    parser.add_argument("--service-account", default="mlp-controlplane-logs")
    parser.add_argument(
        "--workload-image", "--training-image", dest="training_image", required=True
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.execute:
        execute(args)
    else:
        print(
            "Plan: create a project, job and pipeline; verify SSE/RBAC/cancellation; "
            "cancel test runs."
        )


if __name__ == "__main__":
    main()
