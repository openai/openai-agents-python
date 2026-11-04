"""Exercise the public rate-limit snapshot through native HTTP and WebSocket peers."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import BaseModel

from agents import Agent, NativeError, NativeProvider, PromptedOutput, TurnNeedsAttention
from agents.observation import ApiRateLimitsUpdated, EventStream
from agents.rate_limits import ApiRateLimits, ApiRateLimitWindow
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output
from tests.test_native_provider_errors import ProviderErrorPeer, provider_peer as provider_peer
from tests.test_native_provider_websockets import peer as websocket_peer


async def _next_limits(stream: EventStream) -> ApiRateLimitsUpdated:
    async for event in stream:
        if isinstance(event, ApiRateLimitsUpdated):
            return event
    raise AssertionError("The observation stream ended before the rate-limit update")


async def test_http_limits_update_twice_before_the_provider_supplies_usage() -> None:
    send_second = asyncio.Event()
    complete = asyncio.Event()

    async def handle(request: web.Request) -> web.StreamResponse:
        await request.read()
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "X-Ratelimit-Limit-Requests": "60",
                "X-Ratelimit-Remaining-Requests": "0",
                "X-Ratelimit-Reset-Project-Tokens": "125ms",
                "X-Ratelimit-Reset-Requests": "synthetic-unusable-header",
                "Authorization": "Bearer synthetic-not-rate-data",
            }
        )
        await response.prepare(request)
        await response.write(
            b'data: {"type":"response.created","response":{"id":"resp_limits"}}\n\n'
        )
        await send_second.wait()
        update = {
            "type": "response.in_progress",
            "response": {
                "id": "resp_limits",
                "headers": {"X-RateLimit-Reset-Tokens": "1m0.25s"},
            },
        }
        await response.write(f"data: {json.dumps(update)}\n\n".encode())
        await complete.wait()
        for payload in (
            assistant_output("done"),
            {"type": "response.completed", "response": {"id": "resp_limits"}},
        ):
            await response.write(f"data: {json.dumps(payload)}\n\n".encode())
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/responses", handle)
    async with timeout(20), TestServer(app) as server:
        provider = NativeProvider(
            base_url=str(server.make_url("/v1")),
            api_key="synthetic-http-limit-key",
            supports_websockets=False,
        )
        agent = Agent(instructions="Reply.", provider=provider)
        try:
            async with agent.session() as session:
                assert session.rate_limits() == ApiRateLimits()
                stream = session.events()
                turn = await session.submit("Reply.")
                try:
                    first = await _next_limits(stream)
                    assert first.turn_id == turn.turn_id
                    assert (
                        first.snapshot
                        == session.rate_limits()
                        == ApiRateLimits(
                            requests=ApiRateLimitWindow(limit=60, remaining=0),
                            project_tokens=ApiRateLimitWindow(reset_after_seconds=Decimal("0.125")),
                        )
                    )
                    send_second.set()
                    second = await _next_limits(stream)
                    assert second.sequence > first.sequence
                    assert second.turn_id == first.turn_id
                    assert (
                        second.snapshot
                        == session.rate_limits()
                        == ApiRateLimits(
                            tokens=ApiRateLimitWindow(reset_after_seconds=Decimal("60.25"))
                        )
                    )
                finally:
                    send_second.set()
                    complete.set()
                    await stream.aclose()
                assert (await turn.get_result()).final_output == "done"
                assert session.rate_limits() == second.snapshot
        finally:
            await provider.aclose()


async def test_websocket_response_headers_supply_sparse_rate_limits() -> None:
    async with timeout(20), websocket_peer() as peer:
        peer.respond(
            {
                "type": "response.metadata",
                "headers": {
                    "X-Ratelimit-Remaining-Tokens": 11,
                    "X-Ratelimit-Reset-Tokens": "5ms",
                },
            },
            assistant_output("done"),
        )
        agent = Agent(instructions="Reply.", provider=peer.provider())
        async with agent.session() as session:
            stream = session.events()
            turn = await session.submit("Reply.")
            try:
                update = await _next_limits(stream)
                assert update.turn_id == turn.turn_id
                assert (
                    update.snapshot
                    == session.rate_limits()
                    == ApiRateLimits(
                        tokens=ApiRateLimitWindow(
                            remaining=11, reset_after_seconds=Decimal("0.005")
                        )
                    )
                )
                assert (await turn.get_result()).final_output == "done"
            finally:
                await stream.aclose()
        assert peer.upgrades
        assert not peer.http_requests


@pytest.mark.parametrize("blocked", [False, True], ids=["completed", "rate-limited"])
async def test_output_correction_updates_the_parent_rate_limits(blocked: bool) -> None:
    class Result(BaseModel):
        label: str

    calls = 0
    finish_correction = asyncio.Event()

    async def handle(request: web.Request) -> web.StreamResponse:
        nonlocal calls
        await request.read()
        calls += 1
        correction = calls > 1
        if correction and blocked:
            return web.json_response(
                {
                    "error": {
                        "type": "rate_limit_error",
                        "code": "rate_limit_exceeded",
                        "message": "Synthetic correction limit",
                    }
                },
                status=429,
                headers={"Retry-After": "0"},
            )
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "X-RateLimit-Remaining-Requests": "2" if correction else "9",
            }
        )
        await response.prepare(request)
        await response.write(b'data: {"type":"response.created","response":{"id":"repair"}}\n\n')
        if correction:
            await finish_correction.wait()
        for payload in (
            assistant_output('{"label":"fixed"}' if correction else "bad"),
            {"type": "response.completed", "response": {"id": "repair"}},
        ):
            await response.write(f"data: {json.dumps(payload)}\n\n".encode())
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/responses", handle)
    async with timeout(30), TestServer(app) as server:
        provider = NativeProvider(
            base_url=str(server.make_url("/v1")),
            api_key="synthetic-correction-key",
            supports_websockets=False,
        )
        agent = Agent(
            instructions="Return a label.",
            provider=provider,
            output_type=PromptedOutput(Result, max_retries=1),
        )
        async with provider, agent.session() as session:
            stream = session.events()
            turn = await session.submit("Return a label.")
            try:
                initial = await _next_limits(stream)
                assert initial.snapshot == ApiRateLimits(requests=ApiRateLimitWindow(remaining=9))
                if blocked:
                    attention = await turn.wait_for_attention()
                    assert isinstance(attention, TurnNeedsAttention)
                    assert attention.reason == "rate_limited"
                    assert (
                        attention.rate_limits
                        == session.rate_limits()
                        == ApiRateLimits(retry_after_seconds=0)
                    )
                else:
                    observed = await _next_limits(stream)
                    assert observed.parent_submission_id == turn.submission_id
                    assert (
                        observed.snapshot
                        == session.rate_limits()
                        == ApiRateLimits(requests=ApiRateLimitWindow(remaining=2))
                    )
            finally:
                finish_correction.set()
                await stream.aclose()
            if not blocked:
                assert (await turn.get_result()).final_output == Result(label="fixed")
                assert session.rate_limits() == observed.snapshot


@pytest.mark.parametrize(
    ("code", "kind", "category"),
    [
        ("rate_limit_exceeded", "rate_limit_error", "rate_limit_exceeded"),
        ("insufficient_quota", "insufficient_quota", "usage_limit_exceeded"),
    ],
)
async def test_http_failures_retain_retry_advice_and_terminal_category(
    provider_peer: ProviderErrorPeer, code: str, kind: str, category: str
) -> None:
    provider_peer.failure = (429, code, kind)
    provider = NativeProvider(
        base_url=provider_peer.url,
        api_key="synthetic-error-limit-key",
        supports_websockets=False,
    )
    agent = Agent(instructions="Reply.", provider=provider)
    try:
        async with timeout(30), agent.session() as session:
            stream = session.events()
            turn = await session.submit("Reply.")
            try:
                with pytest.raises(NativeError) as caught:
                    await turn.get_result()
                assert caught.value.category == category
                attention = await turn.wait_for_attention()
                assert isinstance(attention, TurnNeedsAttention)
                assert attention.reason == (
                    "quota_blocked" if category == "usage_limit_exceeded" else "rate_limited"
                )
                assert (
                    isinstance(attention.error, NativeError)
                    and attention.error.category == category
                )
                update = await _next_limits(stream)
                assert (
                    update.snapshot == session.rate_limits() == ApiRateLimits(retry_after_seconds=0)
                )
            finally:
                await stream.aclose()
    finally:
        await provider.aclose()
