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


@pytest.mark.parametrize("consumer", ["context", "before", "after", "formatter", "replacement"])
async def test_argument_snapshot_failure_identifies_tool_and_preserves_cause(
    responses: ResponsesFixture, consumer: str
) -> None:
    import threading

    from pydantic import BaseModel, PrivateAttr, model_validator

    from agents import ReplaceArguments
    from tests.test_native_function_namespaces import namespaced_call

    class Payload(BaseModel):
        value: str
        _lock: Any = PrivateAttr(default=None)

        @model_validator(mode="after")
        def create_private_state(self):
            if self.value == "uncopyable":
                self._lock = threading.Lock()
            return self

    entered: list[str] = []

    def before(context: ToolContext[None]):
        entered.append("before")
        return ReplaceArguments({"payload": {"value": "uncopyable"}})

    def after(context, output):
        entered.append("after")
        return output

    def formatter(context, error):
        entered.append("formatter")
        return "expired"

    async def read(payload: Payload) -> str:
        entered.append("body")
        return payload.value

    async def read_with_context(context: ToolContext[None], payload: Payload) -> str:
        return await read(payload)

    callback = read_with_context if consumer == "context" else read
    callback.__annotations__["payload"] = Payload
    tool = function_tool(
        name="read",
        namespace="records",
        before=before if consumer in ("before", "replacement") else None,
        after=after if consumer == "after" else None,
        callback_timeout=1 if consumer == "formatter" else None,
        timeout_behavior="error_as_result",
        timeout_error_function=formatter if consumer == "formatter" else None,
    )(callback)
    responses.respond(
        namespaced_call(
            "records",
            "read",
            {"payload": {"value": "copyable" if consumer == "replacement" else "uncopyable"}},
            "private-snapshot",
        )
    )
    async with (
        timeout(10),
        Agent(
            instructions="Read.", tools=[tool], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("read")
        with pytest.raises(
            TypeError, match=r"records.read.*deep-copyable.*ToolContext.effective_arguments"
        ) as failure:
            await turn.get_result()
        assert isinstance(failure.value.__cause__, TypeError)
        assert "lock" in str(failure.value.__cause__)
    assert entered == (["before"] if consumer == "replacement" else [])
    assert len(responses.requests) == 1


async def test_context_snapshot_keeps_private_model_state_detached(
    responses: ResponsesFixture,
) -> None:
    from pydantic import BaseModel, PrivateAttr

    class Payload(BaseModel):
        value: str
        _history: list[str] = PrivateAttr(default_factory=list)

    snapshots: list[Payload] = []

    async def read(context: ToolContext[None], payload: Payload) -> str:
        snapshots.append(context.effective_arguments["payload"])
        payload._history.append("body")
        return payload.value

    read.__annotations__["payload"] = Payload
    tool = function_tool(read)
    responses.respond(function_call("read", {"payload": {"value": "accepted"}}, "private-copy"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(10),
        Agent(
            instructions="Read.", tools=[tool], provider=provider(responses)
        ).session() as session,
    ):
        assert (await (await session.submit("read")).get_result()).final_output == "done"
    assert len(snapshots) == 1 and snapshots[0]._history == []
