"""One-shot and explicit Sessions both use Core; results and cancellation remain public."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeProvider, function_tool
from agents.observation import OutputTextDelta
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, function_call, tool_output
from tests.test_native_agent_observation import text_response


@dataclass
class CompletedPeer(ResponsesFixture):
    transports: list[asyncio.Transport] = field(default_factory=list)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        assert request.transport is not None
        self.transports.append(request.transport)
        assert body.get("stream") is True
        return await super().handle(request)


@asynccontextmanager
async def completed_peer() -> AsyncIterator[tuple[CompletedPeer, NativeProvider]]:
    peer = CompletedPeer()
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    async with TestServer(app) as server:
        peer.url = str(server.make_url("/v1"))
        provider = NativeProvider(
            base_url=peer.url, api_key="synthetic-one-shot-key", supports_websockets=False
        )
        try:
            yield peer, provider
        finally:
            await provider.aclose()
            async with timeout(5):
                while any(not transport.is_closing() for transport in peer.transports):
                    await asyncio.sleep(0)


async def test_one_shot_preserves_tool_work_usage_and_actual_history() -> None:
    effects = []

    @function_tool
    async def lookup(value: str) -> str:
        """Read one application value."""
        effects.append(value)
        return f"actual:{value}"

    async with timeout(20), completed_peer() as (peer, provider):
        peer.respond(function_call("lookup", {"value": "record"}, "one-shot-tool"))
        peer.respond(assistant_output("answer"))
        result = await Agent(
            instructions="Answer.", model="gpt-5.1-codex", tools=[lookup], provider=provider
        ).run(
            "input",
        )
        assert result.final_output == "answer"
        assert effects == ["record"]
        assert len(peer.requests) == 2
        assert all(request["stream"] is True for request in peer.requests)
        assert tool_output(peer.requests[1], "one-shot-tool") == "actual:record"
        assert result.thread_usage is not None
        assert result.thread_usage.total_token_usage.input_tokens == 22
        assert result.thread_usage.total_token_usage.output_tokens == 6
        assert result.thread_usage.total_token_usage.total_tokens == 28
        assert result.thread_usage.last_token_usage.total_tokens == 14
        assert {headers["authorization"] for headers in peer.request_headers} == {
            "Bearer synthetic-one-shot-key"
        }


@pytest.mark.parametrize("override", ["instance", "class"])
async def test_captured_public_session_keeps_exact_stream_replay(
    monkeypatch: pytest.MonkeyPatch, override: str
) -> None:
    async with timeout(20), completed_peer() as (peer, provider):
        peer.respond(*text_response("stream-item", "first-", "second"))
        agent = Agent(instructions="Answer.", provider=provider)
        original = agent.session
        captured = []

        @asynccontextmanager
        async def capture(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            async with original(**kwargs) as session:
                captured.append(session)
                yield session

        monkeypatch.setattr(agent if override == "instance" else Agent, "session", capture)
        result = await agent.run("input")
        assert result.final_output == "first-second"
        assert peer.requests[0]["stream"] is True
        replay = [
            event.delta
            async for event in captured[0].events()
            if isinstance(event, OutputTextDelta)
            and event.submission_id == result.turn.submission_id
        ]
        assert replay == ["first-", "second"]


async def test_one_shot_uses_route_captured_before_agent_provider_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents import _native

    acquired = asyncio.Event()
    publish = asyncio.Event()
    original_create = _native.create_engine

    async def hold_after_native_acquisition(*args: Any, **kwargs: Any) -> Any:
        native_owner = await original_create(*args, **kwargs)
        acquired.set()
        await publish.wait()
        return native_owner

    monkeypatch.setattr(_native, "create_engine", hold_after_native_acquisition)
    async with timeout(20), completed_peer() as (peer, provider):
        # This provider is only an updated future Agent declaration. The run
        # already opening belongs to the original captured HTTP provider.
        other = NativeProvider(
            base_url=peer.url, api_key="synthetic-future-provider", supports_websockets=True
        )
        peer.respond(assistant_output("original response"))
        agent = Agent(instructions="Answer.", provider=provider)
        task = asyncio.create_task(agent.run("input"))
        try:
            await acquired.wait()
            agent = replace(agent, provider=other)
            publish.set()
            result = await task
            assert result.final_output == "original response"
            assert len(peer.requests) == 1
            assert peer.requests[0]["stream"] is True
            assert peer.request_headers[0]["authorization"] == "Bearer synthetic-one-shot-key"
        finally:
            publish.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await other.aclose()


async def test_cancelled_one_shot_stream_retires_before_provider_close() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    handled = asyncio.Event()
    transports = []
    requests = []

    async def handle(request: web.Request) -> web.StreamResponse:
        requests.append(await request.json())
        assert request.transport is not None
        transports.append(request.transport)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(
            b'data: {"type":"response.created","response":{"id":"held-response"}}\n\n'
        )
        entered.set()
        try:
            await release.wait()
        finally:
            handled.set()
        return response

    app = web.Application()
    app.router.add_post("/v1/responses", handle)
    async with timeout(20), TestServer(app) as server:
        provider = NativeProvider(
            base_url=str(server.make_url("/v1")),
            api_key="synthetic-one-shot-cancel-key",
            supports_websockets=False,
        )
        task = asyncio.create_task(Agent(instructions="Answer.", provider=provider).run("cancel"))
        try:
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await provider.aclose()
            async with timeout(5):
                while any(not transport.is_closing() for transport in transports):
                    await asyncio.sleep(0)
            assert len(requests) == 1 and requests[0]["stream"] is True
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if entered.is_set():
                async with timeout(5):
                    await handled.wait()
            await provider.aclose()
