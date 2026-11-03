"""Verify ownership where input, observation, replacement, and restore meet."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agents import (
    Agent,
    SessionBusyError,
    ToolContext,
    function_tool,
)
from agents._native_async import _NativeCompletionError
from agents.observation import (
    OutputTextDelta,
    SessionEnded,
    ToolFinished,
    ToolStarted,
    TurnFinished,
    TurnStarted,
    UsageUpdated,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    core_owner as core_owner,
    function_call,
    responses as responses,
)
from tests.test_native_agent_observation import collect, provider, text_response
from tests.test_native_session_input import IMAGE, image_content


@dataclass
class Application:
    value: str
    private: str = "private-application-object"


def lookup(calls: list[ToolContext[Application]]):
    @function_tool(name="lookup")
    async def bound(context: ToolContext[Application], value: int) -> str:
        """Read the current application value."""
        calls.append(context)
        return f"{context.context.value}:{value}"

    return bound


async def test_replaced_tools_rich_input_and_observation_survive_native_restore(
    responses: ResponsesFixture,
) -> None:
    original_calls, replaced_calls, restored_calls = [], [], []
    source_context = Application("source-value")
    current_context = Application("current-value")

    @function_tool(name="lookup")
    async def original(context: ToolContext[Application], obsolete: str) -> str:
        original_calls.append(context)
        return obsolete

    source = Agent(
        instructions="Use lookup, then describe the image.",
        tools=[original],
        provider=provider(responses),
        model="gpt-5.5",
    )
    responses.respond(function_call("lookup", {"value": 7}, "source-call"))
    responses.respond(*text_response("source-output", "source ", "answer"))
    async with timeout(30):
        async with source.session(
            context=source_context, tools=[lookup(replaced_calls)]
        ) as session:
            all_source = session.events()
            first = await session.submit(
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "before"},
                        {"type": "input_image", "image_url": IMAGE},
                        {"type": "input_text", "text": "after"},
                    ],
                }
            )
            completed = await (first).get_result()
            source_events = await collect(first.events())
            state = await session.save_state()
            saved_specs = json.dumps(state.managed_tool_specs)
            assert "obsolete" not in saved_specs
            assert '"value"' in saved_specs
            source_items = [item async for item in session.items()]
            source_turns = [item async for item in session.turns()]
            assert completed.final_output == "source answer"
            assert original_calls == []
            assert len(replaced_calls) == 1
            assert replaced_calls[0].context is source_context
            assert replaced_calls[0].submission is first
            assert [type(event) for event in source_events] == [
                TurnStarted,
                ToolStarted,
                ToolFinished,
                UsageUpdated,
                OutputTextDelta,
                OutputTextDelta,
                UsageUpdated,
                TurnFinished,
            ]
            assert source_events[-1].status == "completed"
            encoded = state.to_json()
            for secret in (source_context.private, "synthetic-observation-key"):
                assert secret not in encoded
        assert isinstance((await collect(all_source))[-1], SessionEnded)
        assert await (first).get_result() is completed

        current = Agent(
            instructions=source.instructions,
            tools=[lookup(restored_calls)],
            provider=provider(responses),
            model="gpt-5.5",
        )
        responses.respond(function_call("lookup", {"value": 9}, "restored-call"))
        responses.respond(*text_response("restored-output", "restored ", "answer"))
        async with current.session(state=state, context=current_context) as restored:
            all_restored = restored.events()
            assert restored.id == session.id
            assert [item async for item in restored.items()] == source_items
            assert [item async for item in restored.turns()] == source_turns
            assert (await first.get_result()).turn is first
            assert restored.restored_turn is None
            second = await restored.submit(
                {"role": "user", "content": [{"type": "input_text", "text": "continue"}]}
            )
            assert (await (second).get_result()).final_output == "restored answer"
            events = await collect(second.events())
            assert events[0].sequence == 1
            assert all(event.submission_id == second.submission_id for event in events)
            assert all(event.turn_id == second.turn_id for event in events)
            assert second.turn_id != first.turn_id
            assert len(restored_calls) == len(replaced_calls) == 1
            assert restored_calls[0].context is current_context
            assert restored_calls[0].submission is second
            assert restored_calls[0].call_id == "restored-call"
            resaved = await restored.save_state()
            assert current_context.private not in resaved.to_json()
            assert image_content(responses.requests[2]["input"]) == image_content(
                responses.requests[0]["input"]
            )
            assert "current-value:9" in json.dumps(responses.requests[3])
        drained = await collect(all_restored)
        assert drained[:-1] == events
        assert isinstance(drained[-1], SessionEnded)
        assert len(responses.requests) == 4


@pytest.mark.parametrize("operation", ["save"])
@pytest.mark.parametrize("fail", [False, True])
async def test_mutations_exclude_each_other_and_close_joins_cancelled_owners(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, operation: str, fail: bool
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    closed = asyncio.Event()
    calls = []
    agent = Agent(
        instructions="Answer.",
        provider=provider(responses),
    )
    async with timeout(30), agent.session() as session:
        home = session._home
        events = session.events()
        native = session._native

        class HeldAcknowledgement:
            def __getattr__(self, name: str) -> Any:
                return getattr(native, name)

            async def hold(self, result):
                async def acknowledge():
                    entered.set()
                    await release.wait()
                    if fail:
                        raise _NativeCompletionError("Synthetic lost acknowledgement")
                    return result

                # Model an independently owned completion receipt. A lost ACK
                # takes precedence over cancellation, as at the native boundary.
                receipt = asyncio.create_task(acknowledge())
                cancelled = None
                while True:
                    try:
                        accepted = await asyncio.shield(receipt)
                        break
                    except asyncio.CancelledError as error:
                        if receipt.cancelled():
                            raise
                        cancelled = cancelled or error
                if cancelled is not None:
                    raise cancelled
                return accepted

            async def _replace_tools(self, generation: int, specs: str):
                calls.append("replace")
                return await self.hold(await native._replace_tools(generation, specs))

            async def _export_checkpoint(self):
                calls.append("save")
                return await self.hold(await native._export_checkpoint())

            async def close(self):
                closed.set()
                await native.close()

        monkeypatch.setattr(session, "_native", HeldAcknowledgement())
        mutation = asyncio.create_task(
            session.submit("Select", tools=[]) if operation == "replace" else session.save_state()
        )
        closing = None
        try:
            await entered.wait()
            mutation.cancel("first cancellation")
            await asyncio.sleep(0)
            mutation.cancel("second cancellation")
            await asyncio.sleep(0)
            assert not mutation.done()
            with pytest.raises(RuntimeError):
                await session.submit(
                    {"role": "user", "content": [{"type": "input_text", "text": "must not start"}]}
                )
            with pytest.raises(SessionBusyError):
                await session.submit("Select", tools=[])
            with pytest.raises(RuntimeError, match="mutation|checkpoint"):
                await session.save_state()
            assert calls == [operation]
            closing = asyncio.create_task(session._aclose())
            await asyncio.sleep(0)
            closing.cancel("first close cancellation")
            await asyncio.sleep(0)
            closing.cancel("second close cancellation")
            await asyncio.sleep(0)
            assert not closing.done()
            assert not closed.is_set()
        finally:
            release.set()
        if fail:
            with pytest.raises(_NativeCompletionError, match="Synthetic lost acknowledgement"):
                await mutation
        else:
            with pytest.raises(asyncio.CancelledError):
                await mutation
        assert closing is not None
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert closed.is_set()
        assert session._checkpoint_task is None
        assert session._mutation is None
        assert session._closed and session._reader.done()
        assert session._home is home
        if home is not None:
            assert not Path(home.name).exists()
        assert responses.requests == []
        [ended] = await collect(events)
        assert isinstance(ended, SessionEnded)
        assert ended.reason == "closed"


@pytest.mark.usefixtures("core_owner")
async def test_home_cleanup_failure_ends_observation_and_close_can_retry(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(instructions="Answer.", provider=provider(responses))
    manager = agent.session()
    async with timeout(30):
        session = await manager.__aenter__()
        native_home = session._home
        real_cleanup = native_home.cleanup
        attempts = 0

        def fail_once():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("Synthetic home cleanup failure")
            real_cleanup()

        class FailingHome:
            name = native_home.name

            def cleanup(self):
                fail_once()

        monkeypatch.setattr(session, "_home", FailingHome())
        try:
            events = session.events()
            with pytest.raises(OSError, match="home cleanup"):
                await session._aclose()
            [ended] = await asyncio.wait_for(collect(events), timeout=1)
            assert isinstance(ended, SessionEnded)
            assert ended.reason == "closed"
            assert not session._closed
            assert session._reader.done()
            assert Path(session._home.name).exists()
            await session._aclose()
            assert attempts == 2
            assert session._closed
            assert not Path(session._home.name).exists()
        finally:
            # Preserve cleanup if an assertion fails against an older parent.
            monkeypatch.setattr(session, "_home", native_home)
            real_cleanup()
            try:
                await manager.__aexit__(None, None, None)
            except OSError:
                pass
