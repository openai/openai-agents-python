"""Selected native MCP failures retain diagnostics outside model and raw events."""

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from aiohttp import web

from agents import (
    Agent,
    ApprovalRequest,
    HttpMCP,
    MCPResultContext,
    NativeError,
    PendingRequests,
    TurnStoppedError,
)
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import Peer, mcp_call


class FailedPeer(Peer):
    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("method") == "tools/call":
            self.calls.append(body["params"])
            return web.Response(status=503, text="synthetic-private-transport-failure")
        return await super().handle(request)


def descriptor(url: str) -> HttpMCP[Any]:
    return HttpMCP(
        name="docs",
        url=url,
        tools=["find-record"],
        tool_name_overrides={"find-record": "reviewed"},
        approval_mode="preapproved",
        tool_approval_overrides={"find-record": "prompt"},
    )


def test_failure_policy_selection_preserves_default_and_validates_callback() -> None:
    default = descriptor("http://127.0.0.1:1/mcp")
    assert not default._has_result_callback
    assert replace(default, failure_error_function=None)._has_result_callback
    assert replace(default, failure_error_function=lambda *_: "safe")._has_result_callback
    with pytest.raises(TypeError, match="failure_error_function must be callable"):
        replace(default, failure_error_function="unsafe")  # type: ignore[arg-type]


@pytest.mark.parametrize("policy", ["default", "custom", "raise", "caller_raise"])
async def test_native_mcp_transport_failure_preserves_selected_original_only(
    responses: ResponsesFixture, policy: str
) -> None:
    peer = FailedPeer(names=("find-record",))
    calls: list[Exception] = []
    caller_error = LookupError("synthetic-private-caller-failure")

    def format_failure(context: MCPResultContext[None], error: Exception) -> str:
        assert context.server_name == "docs" and context.tool_name == "find-record"
        assert context.call_id == "failed-transport" and context.arguments == {"key": "43"}
        assert "503" in str(error)
        calls.append(error)
        if policy == "caller_raise":
            raise caller_error
        return "application-approved-failure"

    responses.respond(mcp_call("reviewed", "mcp__docs", "failed-transport"))
    if policy in ("default", "custom"):
        responses.respond(assistant_output("completed after safe failure"))
    native_provider = provider(responses)
    try:
        async with timeout(20), peer.serve() as url:
            selected = descriptor(url)
            if policy == "raise":
                selected = replace(selected, failure_error_function=None)
            elif policy in ("custom", "caller_raise"):
                selected = replace(selected, failure_error_function=format_failure)
            agent = Agent(instructions="Read.", tools=[selected], provider=native_provider)
            async with agent.session(include_raw_provider_events=True) as session:
                turn = await session.submit("Read")
                pending = await turn.wait_for_request_or_result()
                assert isinstance(pending, PendingRequests)
                (request,) = pending.requests
                assert isinstance(request, ApprovalRequest)
                assert not peer.calls and not calls
                await request.respond("allow_once")
                original = None
                if policy in ("raise", "caller_raise"):
                    with pytest.raises(NativeError) as caught:
                        await turn.get_result()
                    original = caught.value.get_mcp_callback_error()
                    if policy == "raise":
                        assert isinstance(original, RuntimeError)
                        assert "503" in str(original)
                    else:
                        assert original is caller_error
                    assert caught.value.__cause__ is None and caught.value.__context__ is None
                    assert str(original) not in str(caught.value)
                    assert len(responses.requests) == 1
                else:
                    assert (await turn.get_result()).final_output == "completed after safe failure"
                    assert len(responses.requests) == 2
                events = [event async for event in turn.events(include_execution_details=True)]
                visible = repr(events) + repr(responses.requests)
                assert "synthetic-private-transport-failure" not in visible
                assert "synthetic-private-caller-failure" not in visible
                if original is not None:
                    assert str(original) not in visible
                assert len(peer.calls) == 1
                assert len(calls) == (1 if policy in ("custom", "caller_raise") else 0)
                if policy == "custom":
                    assert "application-approved-failure" in repr(responses.requests)
                elif policy == "default":
                    assert "MCP tool invocation failed" in repr(responses.requests)
    finally:
        await native_provider.aclose()


async def test_native_mcp_failure_raise_does_not_bypass_original_approval(
    responses: ResponsesFixture,
) -> None:
    peer = FailedPeer(names=("find-record",))
    responses.respond(mcp_call("reviewed", "mcp__docs", "denied-transport"))
    responses.respond(assistant_output("denied before transport"))
    native_provider = provider(responses)
    try:
        async with timeout(20), peer.serve() as url:
            agent = Agent(
                instructions="Read.",
                provider=native_provider,
                tools=[replace(descriptor(url), failure_error_function=None)],
            )
            async with agent.session() as session:
                turn = await session.submit("Read")
                pending = await turn.wait_for_request_or_result()
                assert isinstance(pending, PendingRequests)
                (request,) = pending.requests
                assert isinstance(request, ApprovalRequest)
                await request.respond("deny")
                assert (await turn.get_result()).final_output == "denied before transport"
                assert not peer.calls
    finally:
        await native_provider.aclose()


@pytest.mark.parametrize("ending", ["cancel", "close"])
async def test_native_mcp_failure_policy_does_not_format_cancellation(
    responses: ResponsesFixture, ending: str
) -> None:
    peer = Peer(names=("find-record",), release_call=asyncio.Event())
    calls = []

    def failure(context, error):
        calls.append(error)
        return "must not format cancellation"

    responses.respond(mcp_call("reviewed", "mcp__docs", "cancelled-transport"))
    native_provider = provider(responses)
    try:
        async with timeout(20), peer.serve() as url:
            agent = Agent(
                instructions="Read.",
                provider=native_provider,
                tools=[replace(descriptor(url), failure_error_function=failure)],
            )
            async with agent.session() as session:
                turn = await session.submit("Read")
                pending = await turn.wait_for_request_or_result()
                assert isinstance(pending, PendingRequests)
                (request,) = pending.requests
                assert isinstance(request, ApprovalRequest)
                await request.respond("allow_once")
                await peer.call_entered.wait()
                try:
                    if ending == "cancel":
                        await turn.cancel()
                    else:
                        await session.aclose()
                    with pytest.raises(TurnStoppedError):
                        await turn.get_result()
                    assert not calls
                    assert len(peer.calls) == len(responses.requests) == 1
                finally:
                    peer.release_call.set()
    finally:
        await native_provider.aclose()
