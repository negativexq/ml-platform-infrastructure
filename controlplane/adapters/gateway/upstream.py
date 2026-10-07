"""Where the gateway sends a call: the serving system over HTTP, or (demo, tests) a
ServingProvider in-process."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from time import perf_counter
from typing import Any

import anyio.to_thread
import httpx

from controlplane.adapters.prediction_http import prediction_path
from controlplane.application.gateway import UpstreamCall, UpstreamReply
from controlplane.application.providers import ServingProvider
from controlplane.domain.states import EndpointProtocol


class HttpUpstream:
    """Streams the reply through as it arrives, so long answers (LLM tokens, later) are not
    buffered. MLflow JSON and native V2 requests use their matching backend handlers."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        observe: Callable[[str, float, str], None] | None = None,
    ) -> None:
        self._observe = observe
        self._client = client or httpx.AsyncClient(
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50)
        )

    @property
    def client(self) -> httpx.AsyncClient:
        """The composition root instruments this client, never global HTTPX clients."""
        return self._client

    async def call(self, call: UpstreamCall) -> UpstreamReply:
        if not call.url:
            raise ConnectionError(f"{call.ref} has no address yet")
        _, _, name = call.ref.partition("/")
        if call.protocol is EndpointProtocol.V2_INFER:
            try:
                payload = json.loads(call.body)
            except ValueError:
                payload = None  # Let the backend return its JSON validation error.
            path = prediction_path(name, payload)
        elif call.protocol is EndpointProtocol.OPENAI:
            path = "/openai/v1/chat/completions"  # KServe's Hugging Face server (vLLM)
        else:
            path = "/"  # a function: its own container takes POST / with any JSON
        request = self._client.build_request(
            "POST",
            f"{call.url.rstrip('/')}{path}",
            content=call.body,
            headers={"content-type": "application/json", "x-request-id": call.request_id},
            timeout=httpx.Timeout(call.timeout_seconds, connect=5.0),
        )
        started = perf_counter()
        stages: dict[str, float] = {}

        async def network(event: str, info: Any) -> None:
            stage, _, state = event.rpartition(".")
            if stage not in {"connection.connect_tcp", "connection.start_tls"}:
                return
            if state == "started":
                stages[stage] = perf_counter()
            elif stage in stages and self._observe is not None:
                self._observe(
                    "upstream.connect" if stage.endswith("connect_tcp") else "upstream.tls",
                    perf_counter() - stages.pop(stage),
                    "ok" if state == "complete" else "error",
                )

        if self._observe is not None:
            request.extensions["trace"] = network
        try:
            response = await self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise TimeoutError(str(exc)) from exc
        except httpx.TransportError as exc:
            raise ConnectionError(str(exc)) from exc
        return UpstreamReply(
            status=response.status_code,
            content_type=response.headers.get("content-type", "application/json"),
            chunks=self._observed_stream(response, started),
        )

    async def _observed_stream(
        self, response: httpx.Response, started: float
    ) -> AsyncIterator[bytes]:
        first, outcome = True, "ok"
        try:
            async for chunk in _stream(response):
                if first and self._observe is not None:
                    self._observe("upstream.first_chunk", perf_counter() - started, "ok")
                first = False
                yield chunk
        except BaseException:
            outcome = "error"
            raise
        finally:
            await response.aclose()
            if self._observe is not None:
                self._observe("upstream.total", perf_counter() - started, outcome)

    async def aclose(self) -> None:
        await self._client.aclose()


async def _stream(response: httpx.Response) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_bytes():  # decoded: we do not pass content-encoding on
            yield chunk
    except httpx.TimeoutException as exc:
        raise TimeoutError(str(exc)) from exc
    finally:
        await response.aclose()


class ServingUpstream:
    """Calls a ServingProvider's `predict` (in a worker thread). For the demo and tests,
    where serving is in-process."""

    def __init__(self, serving: ServingProvider) -> None:
        self._serving = serving

    async def call(self, call: UpstreamCall) -> UpstreamReply:
        try:
            payload = json.loads(call.body or b"{}")
        except ValueError:
            return UpstreamReply(400, "application/json", _once(b'{"error": "body is not JSON"}'))
        llm = call.protocol is EndpointProtocol.OPENAI
        answer_of = {
            EndpointProtocol.V2_INFER: self._serving.predict,
            EndpointProtocol.OPENAI: self._serving.chat,
            EndpointProtocol.HTTP: self._serving.invoke,
        }[call.protocol]
        with anyio.fail_after(call.timeout_seconds):
            answer = await anyio.to_thread.run_sync(answer_of, call.ref, payload)
        if llm and payload.get("stream"):
            return UpstreamReply(200, "text/event-stream", _events(dict(answer)))
        return UpstreamReply(200, "application/json", _once(json.dumps(dict(answer)).encode()))


async def _once(body: bytes) -> AsyncIterator[bytes]:
    yield body


async def _events(completion: dict[str, Any]) -> AsyncIterator[bytes]:
    """A finished completion replayed as the server-sent events a streaming runtime sends:
    the text a few words at a time, then the usage, then [DONE]."""
    text = completion["choices"][0]["message"]["content"]
    words = text.split(" ")
    base: dict[str, Any] = {
        "id": completion.get("id"),
        "object": "chat.completion.chunk",
        "model": completion.get("model"),
    }
    for i in range(0, len(words), 4):
        piece = " ".join(words[i : i + 4]) + (" " if i + 4 < len(words) else "")
        delta = {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
        yield f"data: {json.dumps({**base, **delta})}\n\n".encode()
        await anyio.sleep(0)
    done = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    yield f"data: {json.dumps({**base, **done})}\n\n".encode()
    usage: dict[str, Any] = {**base, "choices": [], "usage": completion.get("usage")}
    yield f"data: {json.dumps(usage)}\n\n".encode()
    yield b"data: [DONE]\n\n"
