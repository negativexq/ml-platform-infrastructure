#!/usr/bin/env python3
"""Disposable acceptance pod launcher; never use as a production entrypoint."""

import asyncio
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import uvicorn
from sqlalchemy import create_engine

import controlplane.gateway_main as gateway
from controlplane.persistence.pool import TimedQueuePool

try:
    from controlplane.observability.profile import GatewayTelemetryProfile
except ImportError:  # Replaying older evidence images that predate telemetry profiles.
    GatewayTelemetryProfile = None


def runtime() -> dict:
    root = Path("/sys/fs/cgroup")
    return {
        "at_epoch": time.time(),
        "server_keep_alive_seconds": uvicorn.Config("unused").timeout_keep_alive,
        "active_python_threads": threading.active_count(),
        "memory_peak_bytes": int((root / "memory.peak").read_text()),
        "cpu": {
            key: int(value)
            for key, value in (
                line.split() for line in (root / "cpu.stat").read_text().splitlines()
            )
        },
        "memory_bytes": int((root / "memory.current").read_text()),
        "telemetry_profile": asdict(GatewayTelemetryProfile.from_env())
        if GatewayTelemetryProfile
        else {"latency_sample_rate": 1},
        "otel_sdk_disabled": os.environ.get("OTEL_SDK_DISABLED") == "true",
        "workers": int(os.environ["PROBE_WORKERS"]),
        "pool_capacity": int(os.environ["PROBE_POOL_CAPACITY"]),
    }


class RuntimeHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path == "/telemetry/flush":
            from opentelemetry import metrics

            provider = metrics.get_meter_provider()
            flush = getattr(provider, "force_flush", None)
            ok = flush(timeout_millis=10000) if flush else True
            body = json.dumps({"flushed": ok}).encode()
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if os.environ.get("PROBE_CPU_PROFILE") != "true":
            self.send_error(404)
            return
        import yappi

        if self.path == "/profile/start":
            if yappi.is_running():
                self.send_error(409)
                return
            yappi.clear_stats()
            yappi.set_clock_type("cpu")
            yappi.start(builtins=False, profile_threads=True)
            payload = {"started": True, "clock": "cpu", "at_epoch": time.time()}
        elif self.path == "/profile/stop":
            yappi.stop()
            functions = [
                {
                    "name": stat.name,
                    "module": stat.module,
                    "line": stat.lineno,
                    "calls": stat.ncall,
                    "self_cpu_seconds": stat.tsub,
                    "inclusive_cpu_seconds": stat.ttot,
                }
                for stat in yappi.get_func_stats()
            ]
            payload = {
                "clock": yappi.get_clock_type(),
                "builtins": False,
                "functions": functions,
                "threads": [
                    {
                        "name": stat.name,
                        "cpu_seconds": stat.ttot,
                        "schedule_count": stat.sched_count,
                    }
                    for stat in yappi.get_thread_stats()
                ],
                "scope": (
                    "All Python threads; exclusive CPU includes unprofiled native callees; "
                    "profiler overhead affects performance"
                ),
            }
        else:
            self.send_error(404)
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        body = json.dumps(runtime()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


def factory():
    # The experiment explicitly varies capacity while preserving production timeouts.
    gateway.make_engine = lambda url: create_engine(
        url,
        poolclass=TimedQueuePool,
        pool_size=5,
        max_overflow=int(os.environ["PROBE_POOL_CAPACITY"]) - 5,
        pool_pre_ping=True,
        pool_timeout=3,
        connect_args={"connect_timeout": 3},
    )
    app = gateway.app_factory()
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=int(os.environ["PROBE_WORKERS"]))
        )
        async with original(application):
            yield

    app.router.lifespan_context = lifespan
    return app


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8082), RuntimeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    uvicorn.run(factory, factory=True, host="0.0.0.0", port=8081, access_log=False)
