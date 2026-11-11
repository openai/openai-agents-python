"""Native-acknowledged function responses arrive before the next model completion."""

import asyncio

import pytest

from agents import Agent, NativeProvider, ToolOutput, function_tool
from agents.observation import ToolFinished, TurnFinished
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)


@pytest.mark.parametrize("success", [True, False])
async def test_acknowledged_post_hook_output_is_immediate_redacted_and_reader_isolated(
    responses, success
):
    entered, release = asyncio.Event(), asyncio.Event()

    async def gate(event):
        if event.get("item", {}).get("role") == "assistant":
            entered.set()
            await release.wait()

    responses.before_event = gate

    def after(_context, _output):
        return (
            ToolOutput.text("post-hook output") if success else ToolOutput.error("post-hook output")
        )

    @function_tool(after=after)
    def lookup() -> str:
        return "raw callback output"

    responses.respond(function_call("lookup", {}, "observed-call"))
    responses.respond(assistant_output("done"))
    agent = Agent(
        instructions="Use lookup",
        tools=[lookup],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic-function-output"),
    )
    async with timeout(20), agent.session() as session:
        first = session.events(include_execution_details=True)
        second = session.events(include_execution_details=True)
        ordinary = session.events()
        turn = await session.submit("Lookup")
        try:
            await entered.wait()

            async def finished(reader):
                while True:
                    event = await anext(reader)
                    assert not isinstance(event, TurnFinished)
                    if isinstance(event, ToolFinished):
                        return event

            event = await finished(first)
            assert event.status == ("completed" if success else "failed")
            assert event.output is not None
            assert event.output.content_items == [{"type": "inputText", "text": "post-hook output"}]
            assert event.output.success is success
            assert (event.session_id, event.turn_id, event.submission_id, event.call_id) == (
                session.id,
                turn.turn_id,
                turn.submission_id,
                "observed-call",
            )
            event.output.content_items.clear()
            isolated = await finished(second)
            assert isolated.output is not None
            assert isolated.output.content_items == [
                {"type": "inputText", "text": "post-hook output"}
            ]
            redacted = await finished(ordinary)
            assert redacted.output is None
            assert "post-hook output" not in repr(redacted)
            assert tool_output(responses.requests[1], "observed-call") == "post-hook output"
        finally:
            release.set()
        assert (await turn.get_result()).final_output == "done"


async def test_cancelled_callback_has_no_output_observation(responses):
    entered = asyncio.Event()

    @function_tool
    async def hold() -> str:
        entered.set()
        await asyncio.Future()
        return "unaccepted callback output"

    responses.respond(function_call("hold", {}, "cancelled-call"))
    agent = Agent(
        instructions="Use hold",
        tools=[hold],
        provider=NativeProvider(
            base_url=responses.url, api_key="synthetic-cancelled-function-output"
        ),
    )
    async with timeout(20), agent.session() as session:
        reader = session.events(include_execution_details=True)
        turn = await session.submit("Wait")
        await entered.wait()
        await turn.cancel()
        await session.aclose()
        finished = [event async for event in reader if isinstance(event, ToolFinished)]
        assert len(finished) == 1
        assert finished[0].status == "cancelled"
        assert finished[0].output is None
