"""Tool observations remain independent of execution."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agents import Agent, HttpMCP, StdioMCP, function_tool
from agents.observation import ToolFinished, ToolStarted
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_agent import provider
from tests.test_native_agent_observation import collect


@pytest.mark.parametrize("option", ["on_tool_start", "on_tool_end"])
@pytest.mark.parametrize("family", ["function", "http", "stdio"])
def test_notification_hooks_are_not_declaration_options(option: str, family: str) -> None:
    options: dict[str, Any] = {option: object()}
    with pytest.raises(TypeError, match="unexpected keyword"):
        if family == "function":
            function_tool(**options)
        elif family == "http":
            HttpMCP(name="docs", url="https://example.invalid/mcp", tools=["search"], **options)
        else:
            StdioMCP(name="docs", command="server", tools=["search"], **options)


async def test_observation_failure_does_not_fail_tool_execution(
    responses: ResponsesFixture,
) -> None:
    release = asyncio.Event()
    effects: list[str] = []

    @function_tool
    async def lookup(key: str) -> str:
        await release.wait()
        effects.append(key)
        return "accepted"

    responses.respond(function_call("lookup", {"key": "private-key"}, "observed-function"))
    responses.respond(assistant_output("done"))
    agent = Agent(instructions="Use the tool.", tools=[lookup], provider=provider(responses))
    async with timeout(10), agent.session() as session:
        handle = await session.submit("read")

        async def observe() -> None:
            async for event in session.events():
                if isinstance(event, ToolStarted):
                    raise ValueError("consumer failed")

        try:
            with pytest.raises(ValueError, match="consumer failed"):
                await asyncio.create_task(observe())
            assert effects == []
        finally:
            release.set()
        assert (await handle.get_result()).final_output == "done"
        assert effects == ["private-key"]
        events = await collect(handle.events())
        observed = [event for event in events if isinstance(event, (ToolStarted, ToolFinished))]
        assert [type(event) for event in observed] == [ToolStarted, ToolFinished]
        assert all(event.turn_id == handle.turn_id for event in observed)
        assert "private-key" not in repr(observed)
