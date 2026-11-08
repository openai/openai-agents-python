"""Tool observations remain independent of execution."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agents import Agent, Allow, HttpMCP, StdioMCP, ToolContext, ToolTimeoutError, function_tool
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


@pytest.mark.parametrize("inactive_formatter", [False, True])
async def test_context_free_tool_does_not_copy_private_argument_state(
    responses: ResponsesFixture, inactive_formatter: bool
) -> None:
    import threading

    from pydantic import BaseModel, PrivateAttr

    class Payload(BaseModel):
        value: str
        _lock: Any = PrivateAttr(default_factory=threading.Lock)

    entered: list[str] = []

    def formatter(context: ToolContext[None], error: ToolTimeoutError) -> str:
        raise AssertionError("A formatter without an active deadline must not run")

    async def read(payload: Payload) -> str:
        entered.append(payload.value)
        return payload.value

    # Local annotations must resolve before the decorator captures the schema.
    read.__annotations__["payload"] = Payload
    tool = function_tool(
        timeout_error_function=formatter if inactive_formatter else None,
        timeout_behavior="error_as_result",
    )(read)
    responses.respond(function_call("read", {"payload": {"value": "accepted"}}, "private"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(10),
        Agent(
            instructions="Read.", tools=[tool], provider=provider(responses)
        ).session() as session,
    ):
        assert (await (await session.submit("read")).get_result()).final_output == "done"
    assert entered == ["accepted"]


async def test_tool_context_preserves_received_arguments_before_mutating_validation(
    responses: ResponsesFixture,
) -> None:
    from pydantic import BaseModel, field_validator

    validated: list[list[str]] = []

    class Payload(BaseModel):
        tags: list[str]

        @field_validator("tags", mode="before")
        @classmethod
        def sort_tags(cls, value: list[str]) -> list[str]:
            validated.append(list(value))
            value.sort()
            return value

    seen: list[tuple[list[str], list[str]]] = []

    def before(context: ToolContext[None]) -> Allow:
        seen.append(
            (
                list(context.original_arguments["payload"]["tags"]),
                list(context.effective_arguments["payload"].tags),
            )
        )
        return Allow()

    async def read(payload: Payload) -> str:
        # A callback owns its validated values independently of the policy snapshot.
        payload.tags.append("body")
        return ",".join(payload.tags)

    read.__annotations__["payload"] = Payload
    tool = function_tool(before=before)(read)
    responses.respond(function_call("read", {"payload": {"tags": ["z", "a"]}}, "sorted"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(10),
        Agent(
            instructions="Read.", tools=[tool], provider=provider(responses)
        ).session() as session,
    ):
        assert (await (await session.submit("read")).get_result()).final_output == "done"
    assert validated == [["z", "a"]]
    assert seen == [(["z", "a"], ["a", "z"])]
