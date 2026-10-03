"""Where the gateway sends a call: the serving system over HTTP, or (demo, tests) a
ServingProvider in-process."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import anyio.to_thread
import httpx

from controlplane.application.gateway import UpstreamCall, UpstreamReply
from controlplane.application.providers import ServingProvider
from controlplane.domain.states import EndpointProtocol


class HttpUpstream:
    """Streams the reply through as it arrives, so long answers (LLM tokens, later) are not
    buffered. Model endpoints speak KServe's v2 protocol at `/v2/models/<name>/infer`."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50)
        )

    async def call(self, call: UpstreamCall) -> UpstreamReply:
        if not call.url:
            raise ConnectionError(f"{call.ref} has no address yet")
        _, _, name = call.ref.partition("/")
        if call.protocol is EndpointProtocol.V2_INFER:
            path = f"/v2/models/{name}/infer"
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
        try:
            response = await self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise TimeoutError(str(exc)) from exc
        except httpx.TransportError as exc:
            raise ConnectionError(str(exc)) from exc
        return UpstreamReply(
            status=response.status_code,
            content_type=response.headers.get("content-type", "application/json"),
            chunks=_stream(response),
        )

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
