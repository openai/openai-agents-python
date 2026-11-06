"""Step policy decisions stop their owner before another native request."""

from __future__ import annotations

import asyncio

import pytest

from agents import Agent, StopTurn, ToolContext, TurnStoppedError, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_code_mode import code_call


@pytest.mark.parametrize("synchronous", [False, True])
async def test_one_shot_step_stop_prevents_first_request(
    responses: ResponsesFixture, synchronous: bool, caplog: pytest.LogCaptureFixture
) -> None:
    reason = "synthetic-private-step-policy"

    def stop(turn, *, step_index):
        assert step_index == 0
        return StopTurn(reason)

    responses.respond(assistant_output("must not be requested"))
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    async with timeout(15):
        with pytest.raises(TurnStoppedError, match="Turn stopped by policy") as caught:
            if synchronous:
                await asyncio.to_thread(agent.run_sync, "Answer", before_step=stop)
            else:
                await agent.run("Answer", before_step=stop)

    assert not responses.requests
    error = caught.value
    while error is not None:
        assert reason not in str(error)
        assert reason not in repr(error)
        error = error.__cause__ or error.__context__
    assert reason not in caplog.text


async def test_step_stop_preserves_finished_effect_and_allows_next_turn(
    responses: ResponsesFixture,
) -> None:
    effects = []
    steps = []

    @function_tool
    def lookup() -> str:
        effects.append("record read")
        return "record found"

    async def limit(turn, *, step_index):
        steps.append(step_index)
        await asyncio.sleep(0)
        return StopTurn("One step is enough") if step_index >= 1 else None

    responses.respond(function_call("lookup", {}, "accepted-read"))
    responses.respond(assistant_output("next turn succeeded"))
    async with (
        timeout(15),
        Agent(
            instructions="Use lookup.", tools=[lookup], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("Read the record", before_step=limit)
        with pytest.raises(TurnStoppedError, match="Turn stopped by policy"):
            await turn.get_result()
        assert steps == [0, 1]
        assert effects == ["record read"]
        assert len(responses.requests) == 1
        result = await (await session.submit("Finish the next task")).get_result()
        assert result.final_output == "next turn succeeded"
        assert len(responses.requests) == 2


async def test_cancel_joins_and_discards_late_step_stop(responses: ResponsesFixture) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def late_stop(turn, *, step_index):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            return StopTurn("This decision no longer owns the turn")

    responses.respond(assistant_output("survivor succeeded"))
    async with (
        timeout(15),
        Agent(instructions="Answer briefly.", provider=provider(responses)).session() as session,
    ):
        turn = await session.submit("Wait", before_step=late_stop)
        await entered.wait()
        cancellation = asyncio.create_task(turn.cancel())
        try:
            await cancelled.wait()
            assert not cancellation.done()
            assert not responses.requests
        finally:
            release.set()
            await cancellation
        with pytest.raises(TurnStoppedError, match="^Turn stopped$"):
            await turn.get_result()
        result = await (await session.submit("New work")).get_result()
        assert result.final_output == "survivor succeeded"
        assert len(responses.requests) == 1


async def test_step_stop_retires_yielded_code_before_next_turn(responses: ResponsesFixture) -> None:
    entered = asyncio.Event()
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleaned = asyncio.Event()
    cells = []

    @function_tool
    async def held(ctx: ToolContext[None]) -> str:
        assert ctx.call.cell_id is not None
        cells.append(ctx.call.cell_id)
        entered.set()
        try:
            await asyncio.Event().wait()
            return "unreachable"
        finally:
            cleanup_entered.set()
            await release_cleanup.wait()
            cleaned.set()

    async def stop_after_yield(turn, *, step_index):
        if not responses.requests:
            return None
        await entered.wait()
        return StopTurn("Stop the yielded code too")

    responses.respond(
        code_call(
            "const pending = tools.held({}); yield_control();"
            "try { await pending; } catch (error) {}"
            "await new Promise(resolve => setTimeout(resolve, 60_000));",
            "yielded-cell",
        )
    )
    async with (
        timeout(30),
        Agent(
            instructions="Run the code.",
            tools=[held],
            programmatic_tool_calling=True,
            provider=provider(responses),
        ).session() as session,
    ):
        turn = await session.submit("Start the cell", before_step=stop_after_yield)
        result_task = asyncio.create_task(turn.get_result())
        try:
            await cleanup_entered.wait()
            assert not result_task.done()
        finally:
            release_cleanup.set()
        with pytest.raises(TurnStoppedError, match="^Turn stopped by policy$"):
            await result_task
        assert cleaned.is_set()
        assert len(cells) == 1 and len(responses.requests) == 1
        responses.respond(
            function_call("wait", {"cell_id": cells[0], "yield_time_ms": 1}, "check-stopped-cell")
        )
        responses.respond(assistant_output("next turn succeeded"))
        result = await (await session.submit("Check that the old cell has stopped")).get_result()
        assert result.final_output == "next turn succeeded"
        output = tool_output(responses.requests[2], "check-stopped-cell")
        assert output.startswith(("Script failed", "Script terminated")) or (
            f"exec cell {cells[0]} not found" in output
        )
