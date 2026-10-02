"""Where the gateway sends a call: the serving system over HTTP, or (demo, tests) a
ServingProvider in-process."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

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
        if call.protocol is not EndpointProtocol.V2_INFER:
            raise ConnectionError(f"{call.protocol.value} endpoints are not served yet")
        _, _, name = call.ref.partition("/")
        request = self._client.build_request(
            "POST",
            f"{call.url.rstrip('/')}/v2/models/{name}/infer",
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
        with anyio.fail_after(call.timeout_seconds):
            answer = await anyio.to_thread.run_sync(self._serving.predict, call.ref, payload)
        return UpstreamReply(200, "application/json", _once(json.dumps(dict(answer)).encode()))


async def _once(body: bytes) -> AsyncIterator[bytes]:
    yield body
