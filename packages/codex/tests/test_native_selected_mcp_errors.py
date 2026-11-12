"""Native selected-tool error policy over a real borrowed MCP connection."""

from __future__ import annotations

import asyncio
from dataclasses import fields, replace

import pytest

from agents import Agent, ToolContext, ToolErrorContext, ToolRef, TurnStoppedError
from agents.classic.mcp import MCPServerStreamableHttp
from agents.migration.mcp import MCPNamespace, select_mcp_tools
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import Peer, mcp_call


def _assert_sanitized_error(error: Exception) -> None:
    assert type(error) is RuntimeError
    assert str(error) == "Selected MCP tool invocation failed"
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize(
    "policy", ["custom", "default", "disabled", "constructed-disable", "raise", "cancel"]
)
async def test_borrowed_mcp_retains_native_error_policy(
    responses: ResponsesFixture, policy: str
) -> None:
    peer = Peer()
    context = object()
    calls = []
    defaults = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fallback(call: ToolContext[object], error: Exception) -> str:
        assert call.context is context
        assert call.call_id == "borrowed-failure"
        assert call.call.tool == ToolRef("search", "catalog")
        assert call.original_arguments == {"key": "43"}
        _assert_sanitized_error(error)
        calls.append(call)
        entered.set()
        if policy == "cancel":
            await release.wait()
        return "native custom fallback"

    def default(call) -> str:
        assert call.context is context
        defaults.append(call)
        return "native default fallback"

    responses.respond(mcp_call("search", "catalog", "borrowed-failure"))
    recovered = policy in {"custom", "default"}
    if recovered:
        responses.respond(assistant_output("recovered"))
    async with (
        timeout(30),
        peer.serve() as url,
        MCPServerStreamableHttp(params={"url": url}) as client,
    ):
        [selected] = await select_mcp_tools(
            MCPNamespace("catalog", client), context=context, tool_names=["search"]
        )
        if policy == "constructed-disable":
            captured = {
                item.name: getattr(selected, item.name)
                for item in fields(selected)
                if item.name not in {"error_function", "_allow_run_error_formatter"}
            }
            selected = type(selected)(**captured, error_function=None)
        elif policy != "default":
            selected = replace(selected, error_function=fallback)
            if policy in {"disabled", "raise"}:
                selected = replace(selected, error_function=None)
        peer.denied = True
        agent = Agent(
            instructions="Read one record.", provider=provider(responses), tools=[selected]
        )
        async with agent.session(
            context=context, tool_error_formatter=default if policy != "raise" else None
        ) as session:
            turn = await session.submit("Read")
            if policy == "cancel":
                await entered.wait()
                stopping = asyncio.create_task(turn.cancel())
                release.set()
                await stopping
                with pytest.raises(TurnStoppedError):
                    await turn.get_result()
            elif recovered:
                assert (await turn.get_result()).final_output == "recovered"
                output = tool_output(responses.requests[1], "borrowed-failure")
                assert (
                    "native custom fallback" if policy == "custom" else "native default fallback"
                ) in output
                assert peer.label not in output
            else:
                with pytest.raises(RuntimeError, match="Selected MCP tool invocation failed"):
                    await turn.get_result()
        peer.denied = False
        assert (await client.list_tools())[0].name == "search"
        assert (
            len([request for request in peer.requests if request.get("method") == "tools/call"])
            == 1
        )
    assert len(calls) == (policy in {"custom", "cancel"})
    assert len(defaults) == (policy == "default")


@pytest.mark.parametrize("policy", ["throws", "cancel"])
async def test_borrowed_mcp_retains_session_formatter_failure_policy(
    responses: ResponsesFixture, policy: str
) -> None:
    peer_detail = "synthetic-private-peer-detail"
    formatter_detail = "synthetic-private-formatter-detail"
    peer = Peer(call_result={"content": [{"type": "text", "text": {"detail": peer_detail}}]})
    context = object()
    entered = asyncio.Event()
    exited = asyncio.Event()
    calls = []

    async def formatter(failure: ToolErrorContext[object]) -> str:
        _assert_sanitized_error(failure.error)
        assert failure.context is context
        assert failure.call.tool == ToolRef("search", "catalog")
        assert failure.phase == "execution"
        calls.append(failure)
        if policy == "throws":
            raise RuntimeError(formatter_detail)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()
        pytest.fail("The blocked formatter unexpectedly resumed")

    responses.respond(mcp_call("search", "catalog", "borrowed-formatter-failure"))
    if policy == "throws":
        responses.respond(assistant_output("recovered"))
    async with (
        timeout(30),
        peer.serve() as url,
        MCPServerStreamableHttp(params={"url": url}) as client,
    ):
        [selected] = await select_mcp_tools(
            MCPNamespace("catalog", client), context=context, tool_names=["search"]
        )
        agent = Agent(instructions="Read.", provider=provider(responses), tools=[selected])
        async with agent.session(context=context, tool_error_formatter=formatter) as session:
            turn = await session.submit("Read")
            if policy == "throws":
                assert (await turn.get_result()).final_output == "recovered"
                output = tool_output(responses.requests[1], "borrowed-formatter-failure")
                assert output == "Function 'search' failed."
                assert peer_detail not in output and formatter_detail not in output
            else:
                outcome = asyncio.create_task(turn.get_result())
                ready = asyncio.create_task(entered.wait())
                try:
                    await asyncio.wait((outcome, ready), return_when=asyncio.FIRST_COMPLETED)
                    if not entered.is_set():
                        await outcome
                        pytest.fail("The Session formatter was never entered")
                    await turn.cancel()
                    assert exited.is_set()
                    with pytest.raises(TurnStoppedError):
                        await outcome
                finally:
                    ready.cancel()
                    outcome.cancel()
                    await asyncio.gather(ready, outcome, return_exceptions=True)
        assert (await client.list_tools())[0].name == "search"

    assert peer.calls == [{"name": "search", "arguments": {"key": "43"}}]
    assert len(calls) == 1
    assert len(responses.requests) == (2 if policy == "throws" else 1)
    assert not responses.replies
