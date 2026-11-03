import asyncio

import pytest

from agents import Agent, TurnStoppedError
from agents.testing import ScriptedResponses, text, tool_call
from examples.recipes.safe_effects import Application, FakeWriter, main, write_record


async def test_fake_writer_deduplicates_real_tool_execution():
    await main()


@pytest.mark.parametrize("mode", ["error", "raise"])
async def test_failure_does_not_undo_write_and_session_can_continue(mode):
    fake = FakeWriter()
    app = Application(fake)
    scripts = [[tool_call("write_record", {"key": "synthetic-a", "mode": mode}, call_id="write")]]
    if mode == "error":
        scripts.append([text("Tool reported an error.")])
    scripts.append([text("Continued.")])
    async with ScriptedResponses(*scripts) as peer:
        agent = Agent(instructions="Write.", tools=[write_record], provider=peer.provider)
        async with agent.session(context=app) as session:
            turn = await session.submit("Write.")
            if mode == "raise":
                with pytest.raises(ValueError, match="Synthetic exception"):
                    await turn.get_result()
                assert peer.tool_output("write") is None
            else:
                await turn.get_result()
                assert peer.tool_output("write") == "Synthetic failure after write"
                items = [item async for item in session.items()]
                assert any(item["type"] == "function_call_output" for item in items)
            assert fake.ledger == ["synthetic-a"]
            continued = await session.submit("Continue.")
            assert (await continued.get_result()).final_output == "Continued."
            assert fake.ledger == ["synthetic-a"]


async def test_cancel_reader_then_turn_joins_cleanup_and_allows_continuation():
    app = Application(FakeWriter())
    async with ScriptedResponses(
        [tool_call("write_record", {"key": "synthetic-a", "mode": "wait"}, call_id="write")],
        [text("Continued.")],
    ) as peer:
        agent = Agent(instructions="Write.", tools=[write_record], provider=peer.provider)
        async with agent.session(context=app) as session:
            turn = await session.submit("Write.")
            await asyncio.wait_for(app.entered.wait(), 10)
            reader_entered = asyncio.Event()

            async def read():
                reader_entered.set()
                return await turn.get_result()

            reader = asyncio.create_task(read())
            await reader_entered.wait()
            reader.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reader
            assert not app.cleanup_started.is_set()  # Cancelling a reader leaves work running.
            stopping = asyncio.create_task(turn.cancel())
            await asyncio.wait_for(app.cleanup_started.wait(), 10)
            try:
                assert not stopping.done() and not app.cleaned.is_set()
            finally:
                app.cleanup_release.set()
            await asyncio.wait_for(stopping, 10)
            assert app.cleaned.is_set()
            with pytest.raises(TurnStoppedError):
                await turn.get_result()
            continued = await session.submit("Continue.")
            assert (await continued.get_result()).final_output == "Continued."
            assert app.writer.ledger == ["synthetic-a"]
            assert "aborted by user" in peer.tool_output("write")
            assert peer.tool_output("write") != "written"
