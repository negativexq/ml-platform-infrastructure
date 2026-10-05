"""Budget stays reserved while streams run, including missing usage and cancellation."""

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import Mock
from uuid import uuid4

import pytest

from controlplane.adapters.gateway.limits import TokenBucketLimiter
from controlplane.application.gateway import Caller, GatewayService, Route, UpstreamReply
from controlplane.application.llm import MAX_METERED_BYTES, TokenMeter, reserve_chat
from controlplane.domain.entities import EndpointLimits
from controlplane.domain.states import EndpointKind, EndpointProtocol, EndpointStatus


def test_inflight_missing_usage_and_interrupted_streams_keep_reservation() -> None:
    async def scenario() -> None:
        limiter = TokenBucketLimiter(lambda: 0)
        recorder = Mock()
        service = GatewayService(Mock(), Mock(), limiter, [recorder], monotonic=lambda: 0)
        route = Route(
            uuid4(),
            "p",
            "e",
            EndpointStatus.READY,
            EndpointKind.LLM,
            EndpointProtocol.OPENAI,
            EndpointLimits(units_per_minute=100),
            "ns/e",
            None,
            None,
        )
        caller = Caller("caller")
        buckets = service._buckets(caller, route)
        assert limiter.take(buckets, 80).allowed
        assert not limiter.take(buckets, 80).allowed  # another in-flight request

        async def missing_usage() -> AsyncIterator[bytes]:
            yield b'{"choices": []}'

        reply = UpstreamReply(200, "application/json", missing_usage())
        assert [chunk async for chunk in service._counted(reply, caller, route, 0, 80)]
        assert recorder.record.call_args.args[0].units == 80
        assert not limiter.take(buckets, 21).allowed

        limiter.refund(buckets, 80)
        assert limiter.take(buckets, 80).allowed

        async def interrupted() -> AsyncIterator[bytes]:
            yield b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2}}\n\n'
            raise asyncio.CancelledError()

        reply = UpstreamReply(200, "text/event-stream", interrupted())
        with pytest.raises(asyncio.CancelledError):
            async for _ in service._counted(reply, caller, route, 0, 80):
                pass
        assert recorder.record.call_args.args[0].units == 80
        assert not limiter.take(buckets, 21).allowed

    asyncio.run(scenario())


def test_complete_reported_usage_refunds_only_unused_reservation() -> None:
    async def scenario() -> None:
        limiter = TokenBucketLimiter(lambda: 0)
        service = GatewayService(Mock(), Mock(), limiter, monotonic=lambda: 0)
        route = Route(
            uuid4(),
            "p",
            "e",
            EndpointStatus.READY,
            EndpointKind.LLM,
            EndpointProtocol.OPENAI,
            EndpointLimits(units_per_minute=100),
            "ns/e",
            None,
            None,
        )
        caller = Caller("caller")
        buckets = service._buckets(caller, route)
        assert limiter.take(buckets, 80).allowed

        async def chunks() -> AsyncIterator[bytes]:
            yield b'{"usage":{"prompt_tokens":10,"completion_tokens":5}}'

        async for _ in service._counted(
            UpstreamReply(200, "application/json", chunks()), caller, route, 0, 80
        ):
            pass
        assert limiter.take(buckets, 85).allowed
        assert not limiter.take(buckets, 1).allowed

    asyncio.run(scenario())


def test_meter_rejects_malformed_counts_and_bounds_sse_memory() -> None:
    meter = TokenMeter(streamed=True)
    meter.feed(b'data: {"usage":{"prompt_tokens":"oops","completion_tokens":-2}}\n')
    assert not meter.usage_reported
    meter.feed(b"x" * (MAX_METERED_BYTES + 1))
    meter.finish()
    assert len(meter._pending) == 0 and not meter.usage_reported
    body, reserved = reserve_chat(b'{"messages":[{"role":"user","content":"hi"}]}', 500)
    assert b'"max_tokens":' in body and 0 < reserved <= 500
