"""Real HTTP through gateway and stand-in upstream; no PostgreSQL/browser/cluster."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from controlplane.adapters.gateway import HttpUpstream, TokenBucketLimiter
from controlplane.application.api_access import ApiAccessService
from controlplane.application.gateway import GatewayService
from controlplane.domain.entities import EndpointLimits
from controlplane.domain.states import Exposure
from controlplane.gateway import create_gateway
from controlplane.persistence.memory import MemoryStore, MemoryUnitOfWork
from controlplane.tests.conftest import FakeClock
from controlplane.tests.test_functions import Env as FunctionEnv
from controlplane.tests.test_gateway import Monotonic, Recorder
from controlplane.tests.test_llm import Env as LlmEnv


@contextmanager
def serve(app: FastAPI) -> Iterator[str]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", loop="asyncio"))
        worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started:
                if not worker.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("HTTP test server did not start")
                time.sleep(0.01)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            worker.join(5)
            assert not worker.is_alive(), "HTTP server failed to stop"


def test_real_http_function_and_llm_streaming_admission() -> None:
    app = FastAPI()
    release, arrived = threading.Event(), threading.Event()
    seen: list[tuple[str, dict[str, Any], str | None]] = []

    @app.post("/")
    async def invoke(request: Request) -> dict[str, Any]:
        body = await request.json()
        seen.append(("/", body, request.headers.get("x-request-id")))
        return {"received": body}

    @app.post("/openai/v1/chat/completions")
    async def chat(request: Request) -> Any:
        body = await request.json()
        seen.append((request.url.path, body, request.headers.get("x-request-id")))
        mode = body["messages"][0]["content"]
        if not body.get("stream"):
            return {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 2}}

        async def chunks() -> Any:
            yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
            arrived.set()
            if mode == "hold":
                await asyncio.to_thread(release.wait, 3)
            if mode != "missing":
                yield b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":2}}\n\n'
            yield b"data: [DONE]\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    store, clock = MemoryStore(), FakeClock()

    def factory() -> MemoryUnitOfWork:
        return MemoryUnitOfWork(store)

    function = FunctionEnv(factory, clock)
    function.serve()
    llm = LlmEnv(factory, clock)
    llm.version()
    llm.serve()
    access = ApiAccessService(factory, clock)
    for name in ("ticket-router", "assistant-prod"):
        access.expose("support", name, Exposure.PUBLIC, EndpointLimits(units_per_minute=300))
    token = access.create_key(
        "support", name="http-test", endpoints=["ticket-router", "assistant-prod"]
    )[1]
    monotonic, usage = Monotonic(), Recorder()
    upstream = HttpUpstream()
    gateway = create_gateway(
        GatewayService(
            factory,
            upstream,
            TokenBucketLimiter(monotonic),
            clock=clock,
            monotonic=monotonic,
            recorders=[usage],
        )
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Any:
        try:
            yield
        finally:
            await upstream.aclose()

    gateway.router.lifespan_context = lifespan
    with serve(app) as upstream_url:
        with factory() as uow:
            project = uow.projects.get_by_name("support")
            assert project is not None
            for name in ("ticket-router", "assistant-prod"):
                endpoint = uow.endpoints.get_by_name(project.id, name)
                assert endpoint is not None
                uow.endpoints.update(
                    replace(endpoint, url=upstream_url), expected_status=endpoint.status
                )
            uow.commit()
        with serve(gateway) as url, httpx.Client(base_url=url, timeout=5) as client:
            headers = {"authorization": "Bearer " + token, "x-request-id": "real-http-test"}
            answer = client.post(
                "/v1/support/ticket-router/invoke", json={"ticket": 42}, headers=headers
            )
            assert answer.status_code == 200 and answer.json() == {"received": {"ticket": 42}}
            assert seen[-1] == ("/", {"ticket": 42}, "real-http-test")
            path = "/v1/support/assistant-prod/chat/completions"

            def payload(mode: str, stream: bool = True) -> dict[str, Any]:
                return {
                    "messages": [{"role": "user", "content": mode}],
                    "max_tokens": 100,
                    "stream": stream,
                }

            assert (
                client.post(path, json=payload("valid", False), headers=headers).status_code == 200
            )
            monotonic.t += 60
            try:
                with client.stream("POST", path, json=payload("hold"), headers=headers) as stream:
                    assert stream.status_code == 200
                    lines = stream.iter_lines()
                    assert next(lines).startswith("data:")
                    assert arrived.wait(2)
                    assert not release.is_set()  # first event arrived before completion
                    denied = client.post(path, json=payload("valid", False), headers=headers)
                    assert denied.status_code == 429 and "retry-after" in denied.headers
                    release.set()
                    assert "[DONE]" in "\n".join(lines)
            finally:
                release.set()
            assert (
                client.post(path, json=payload("valid", False), headers=headers).status_code == 200
            )
            monotonic.t += 60
            missing = client.post(path, json=payload("missing"), headers=headers)
            assert missing.status_code == 200 and "[DONE]" in missing.text
            assert (
                client.post(path, json=payload("valid", False), headers=headers).status_code == 429
            )
            monotonic.t += 60
            release.clear()
            arrived.clear()
            previous_calls = len(usage.calls)
            try:
                with client.stream("POST", path, json=payload("hold"), headers=headers) as stream:
                    assert stream.status_code == 200
                    lines = stream.iter_lines()
                    assert next(lines).startswith("data:")
                    assert arrived.wait(2)
                    # Closing before usage arrives must keep the reservation.
            finally:
                release.set()
            deadline = time.monotonic() + 2
            while len(usage.calls) == previous_calls and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(usage.calls) == previous_calls + 1
            assert usage.calls[-1].units > 100
            assert (
                client.post(path, json=payload("valid", False), headers=headers).status_code == 429
            )
            forwarded: dict[str, Any] = dict(seen[-1][1])
            assert forwarded["model"] == "assistant-prod"
            assert forwarded["stream_options"] == {"include_usage": True}
            assert any(call.units == 13 for call in usage.calls)
            assert usage.calls[-2].units > 100  # no usage retains prompt/output reservation
