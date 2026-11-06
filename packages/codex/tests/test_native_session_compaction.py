"""Managed compaction uses Core history, task ownership and native events."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace

import pytest
from pydantic import BaseModel

from agents import Agent, NativeError, NativeProvider, ToolContext, function_tool
from agents.observation import ContextCompacted, SessionEnded, ToolStarted, TurnFinished
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    core_owner as core_owner,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent_lifecycle import task_cancellation


def provider(peer: ResponsesFixture, key: str = "synthetic-compaction-key") -> NativeProvider:
    return NativeProvider(base_url=peer.url, api_key=key)


class Answer(BaseModel):
    answer: str


@dataclass
class Application:
    private_value: str
    calls: list[str] = field(default_factory=list)


async def test_compact_native_history_events_checkpoint_and_current_authority(
    responses: ResponsesFixture, monkeypatch
) -> None:
    from agents import _native

    native_compactions = []
    original_next = _native.NativeThread._next_session_event

    async def capture_next(thread):
        value = await original_next(thread)
        event = json.loads(value)
        if event and event["msg"]["type"] == "item_completed":
            message = event["msg"]
            if message["item"]["type"] == "ContextCompaction":
                native_compactions.append(message)
        return value

    monkeypatch.setattr(_native.NativeThread, "_next_session_event", capture_next)

    @function_tool
    async def lookup(context: ToolContext[Application]) -> str:
        context.context.calls.append(context.submission.turn_id)
        return context.context.private_value

    original = Application("original-private-context")
    current = Application("current-private-context")
    agent: Agent[Application, Answer] = Agent(
        instructions="Use lookup when asked. Remember atlas.",
        tools=[lookup],
        output_type=Answer,
        model="gpt-5.5",
        provider=provider(responses),
    )
    responses.respond(assistant_output('{"answer":"remembered"}'))
    responses.respond(assistant_output("Native summary: the remembered label is atlas."))
    responses.respond(function_call("lookup", {}, "restored-lookup"))
    responses.respond(assistant_output('{"answer":"restored"}'))
    async with timeout(20):
        async with agent.session(context=original) as session:
            stream = session.events()
            handle = await session.submit("Remember atlas.")
            first = await (handle).get_result()
            assert first.final_output == Answer(answer="remembered")
            assert await session.compact() is None
            assert await (handle).get_result() is first
            items = [item async for item in session.items()]
            turns = [item async for item in session.turns()]
            assert [turn["status"] for turn in turns] == ["completed", "completed"]
            summaries = [item for item in items if item["turn_id"] == turns[1]["id"]]
            assert len(summaries) == 1
            assert summaries[0]["type"] == "message"
            assert summaries[0]["role"] == "assistant"
            assert summaries[0]["content"][0]["text"] == (
                "Native summary: the remembered label is atlas."
            )
            state = await session.save_state()
            assert any(record["type"] == "compacted" for record in state.history)
            saved = state.to_json()
            assert state.agent_options == {}
            for private in (original.private_value, "synthetic-compaction-key", "callback"):
                assert private not in saved
        events = [event async for event in stream]
        compactions = [event for event in events if isinstance(event, ContextCompacted)]
        assert len(compactions) == 1
        assert len(native_compactions) == 1
        assert compactions[0].item_id == native_compactions[0]["item"]["id"]
        assert compactions[0].turn_id == native_compactions[0]["turn_id"] == turns[1]["id"]
        assert compactions[0].session_id == native_compactions[0]["thread_id"]
        assert not any(isinstance(event, ToolStarted) for event in events)
        settled = [event for event in events if isinstance(event, TurnFinished)]
        assert [event.status for event in settled] == ["completed", "completed"]

        def forbidden_start(*args, **kwargs):
            pytest.fail("Unverifiable compacted instructions reached native acquisition")

        for change in ("missing_replacement", "missing_provenance", "changed_instructions"):
            raw = json.loads(saved)
            compact = next(row["payload"] for row in raw["history"] if row["type"] == "compacted")
            if change == "missing_replacement":
                compact.pop("replacement_history")
                compact.pop("replacement_history_metadata")
            else:
                changed = False
                for item in compact["replacement_history"]:
                    metadata = item.get("internal_chat_message_metadata_passthrough", {})
                    kinds = metadata.get("content_item_kinds", [])
                    owned = next(
                        (kind for kind in kinds if kind.startswith("sdk_application.")), None
                    )
                    if owned is None:
                        continue
                    changed = True
                    if change == "missing_provenance":
                        item.pop("internal_chat_message_metadata_passthrough")
                    else:
                        item["content"][kinds.index(owned)]["text"] = (
                            "Different application authority."
                        )
                assert changed
            with monkeypatch.context() as patch:
                patch.setattr(_native, "create_checkpoint_engine", forbidden_start)
                with pytest.raises(ValueError, match="instructions"):
                    async with agent.session(
                        context=current, state=SessionState.from_json(json.dumps(raw))
                    ):
                        pytest.fail("Unverifiable compacted state restored")

        agent = replace(agent, provider=provider(responses, "current-compaction-key"))
        async with agent.session(context=current, state=state) as restored:
            assert [item async for item in restored.items()] == items
            assert [turn["id"] for turn in [item async for item in restored.turns()]] == [
                turn["id"] for turn in turns
            ]
            result = await (await restored.submit("Use lookup now.")).get_result()
            assert result.final_output == Answer(answer="restored")
        assert original.calls == []
        assert current.calls == [result.turn.turn_id]
        assert len(responses.requests) == 4
        assert responses.requests[1].get("tools", []) == []
        assert responses.requests[1].get("text", {}).get("format", {}).get("type") != "json_schema"
        assert "Native summary: the remembered label is atlas." in json.dumps(
            responses.requests[2]["input"]
        )
        assert tool_output(responses.requests[3], "restored-lookup") == current.private_value
        assert responses.request_headers[2]["authorization"] == "Bearer current-compaction-key"


async def test_fresh_and_repeated_compaction_can_be_saved_and_restored(
    responses: ResponsesFixture,
) -> None:
    responses.respond(assistant_output("First native summary."))
    responses.respond(assistant_output("Second native summary."))
    responses.respond(assistant_output("continued"))
    agent = Agent(instructions="Preserve this instruction.", provider=provider(responses))
    async with timeout(20):
        async with agent.session() as session:
            await session.compact()
            await session.compact()
            state = await session.save_state()
            assert len([row for row in state.history if row["type"] == "compacted"]) == 2
        async with agent.session(state=state) as restored:
            assert (await (await restored.submit("Continue.")).get_result()).final_output == (
                "continued"
            )
        assert len(responses.requests) == 3


@pytest.mark.usefixtures("core_owner")
async def test_compact_rejects_active_work_and_raw_bypass(responses: ResponsesFixture) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(event):
        if event["type"] == "response.created":
            entered.set()
            await release.wait()

    responses.before_event = hold
    responses.respond(assistant_output("ordinary answer"))
    async with timeout(15):
        async with Agent(instructions="Answer.", provider=provider(responses)).session() as session:
            handle = await session.submit("ordinary input")
            await entered.wait()
            try:
                with pytest.raises(RuntimeError, match="active"):
                    await session.compact()
                # The private binding repeats the native idle guard. Raw Compact
                # remains blocked because Core would replace the active turn.
                receipt = json.loads(await session._native._compact())
                assert receipt["status"] == "not_submitted"
                with pytest.raises(RuntimeError, match="unsupported"):
                    await session._native.call("compact")
                assert len(responses.requests) == 1
            finally:
                release.set()
            assert (await (handle).get_result()).final_output == "ordinary answer"


@pytest.mark.parametrize("operation", ["replace_tools", "save_state", "update"])
async def test_compact_rejects_an_owned_mutation_before_native_io(
    responses: ResponsesFixture, monkeypatch, operation: str
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    method = {
        # A Python-only operation subset keeps the bound generation. Its native
        # admission, not generation replacement, is now the exclusion boundary.
        "replace_tools": "_submit",
        "save_state": "_export_checkpoint",
        "update": "_update_model_settings",
    }[operation]
    if operation == "replace_tools":
        responses.respond(assistant_output("selected operation"))
    async with timeout(15):
        async with Agent(instructions="Answer.", provider=provider(responses)).session() as session:
            owner_type = type(session._native)
            original = getattr(owner_type, method)

            async def hold(owner, *args):
                entered.set()
                await release.wait()
                return await original(owner, *args)

            # The chosen backend owns mutation until any required promotion settles.
            monkeypatch.setattr(owner_type, method, hold)
            pending = asyncio.create_task(
                session.submit("new", tools=[])
                if operation == "replace_tools"
                else session.update(model_settings={"reasoning_effort": "high"})
                if operation == "update"
                else session.save_state()
            )
            await entered.wait()
            try:
                with pytest.raises(
                    RuntimeError,
                    match="active" if operation == "replace_tools" else "mutation|checkpoint",
                ):
                    await session.compact()
                assert responses.requests == []
            finally:
                release.set()
                result = await pending
                if operation == "replace_tools":
                    assert (await result.get_result()).final_output == "selected operation"


@pytest.mark.parametrize("stop", ["cancel", "close"])
async def test_compaction_excludes_mutations_and_joins_interrupt_cleanup(
    responses: ResponsesFixture, monkeypatch, stop: str
) -> None:
    from agents import _native

    entered, release_response = asyncio.Event(), asyncio.Event()
    interrupt, release_interrupt = asyncio.Event(), asyncio.Event()
    original = _native.NativeThread._interrupt_session

    async def hold_response(event):
        if event["type"] == "response.created":
            entered.set()
            await release_response.wait()

    async def hold_interrupt(thread, turn_id, *, queued_compaction=False):
        interrupt.set()
        await release_interrupt.wait()
        return await original(thread, turn_id, queued_compaction=queued_compaction)

    responses.before_event = hold_response
    responses.respond(assistant_output("summary that must not finish"))
    monkeypatch.setattr(_native.NativeThread, "_interrupt_session", hold_interrupt)
    async with timeout(15):
        async with Agent(instructions="Answer.", provider=provider(responses)).session() as session:
            stream = session.events()
            pending = asyncio.create_task(session.compact())
            await entered.wait()
            try:
                for conflicting in (
                    lambda: session.submit("conflict"),
                    session.compact,
                    lambda: session.submit("new", tools=[]),
                    lambda: session.update(model_settings={"reasoning_effort": "high"}),
                    session.save_state,
                ):
                    with pytest.raises(RuntimeError, match="active|busy|requires an idle Session"):
                        await conflicting()
                if stop == "cancel":
                    pending.cancel("first cancellation")
                    await interrupt.wait()
                    pending.cancel("second cancellation")
                closing = asyncio.create_task(session._aclose())
                await interrupt.wait()
                await asyncio.sleep(0)
                assert not pending.done()
                assert not closing.done()
            finally:
                release_interrupt.set()
                release_response.set()
            if stop == "cancel":
                with pytest.raises(asyncio.CancelledError) as cancelled:
                    await pending
                assert task_cancellation(cancelled.value).args == ("first cancellation",)
            else:
                from agents import TurnStoppedError

                with pytest.raises(TurnStoppedError):
                    await pending
            await closing
        events = [event async for event in stream]
        assert isinstance(events[-1], SessionEnded)
        assert [event.status for event in events if isinstance(event, TurnFinished)] == [
            "cancelled"
        ]
        assert len(responses.requests) == 1


async def test_native_compaction_error_is_not_decoded_as_agent_output(
    responses: ResponsesFixture,
) -> None:
    # Core retries compaction streams five times, including invalid-prompt
    # errors. Keep every response identical so we test the terminal native error.
    for _ in range(6):
        responses.respond(
            {
                "type": "response.failed",
                "response": {
                    "error": {
                        "type": "invalid_request_error",
                        "code": "invalid_prompt",
                        "message": "compaction rejected by synthetic provider",
                    }
                },
            }
        )
    async with timeout(30):
        async with Agent(
            instructions="Answer.", output_type=Answer, provider=provider(responses)
        ).session() as session:
            with pytest.raises(NativeError, match="compaction rejected"):
                await session.compact()
            assert len(responses.requests) == 6


async def test_compaction_cancellation_keeps_other_session_and_callback_context_isolated(
    responses: ResponsesFixture,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    left, right = Application("left"), Application("right")

    @function_tool
    async def lookup(context: ToolContext[Application]) -> str:
        context.context.calls.append(context.submission.turn_id)
        return context.context.private_value

    async def hold_first(event):
        if len(responses.requests) == 1 and event["type"] == "response.created":
            entered.set()
            await release.wait()

    responses.before_event = hold_first
    responses.respond(assistant_output("interrupted summary"))
    responses.respond(function_call("lookup", {}, "right-lookup"))
    responses.respond(assistant_output("right complete"))
    responses.respond(function_call("lookup", {}, "left-lookup"))
    responses.respond(assistant_output("left complete"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    async with timeout(20):
        async with agent.session(context=left) as first, agent.session(context=right) as second:
            pending = asyncio.create_task(first.compact())
            await entered.wait()
            try:
                result = await (await second.submit("Use lookup.")).get_result()
                assert result.final_output == "right complete"
                assert right.calls == [result.turn.turn_id]
                assert left.calls == []
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                assert [item async for item in first.turns()][-1]["status"] == "cancelled"
                assert not any(
                    item["type"] == "message" and item["role"] == "assistant"
                    for item in [item async for item in first.items()]
                )
            finally:
                release.set()
            result = await (await first.submit("Use lookup.")).get_result()
            assert result.final_output == "left complete"
            assert left.calls == [result.turn.turn_id]
            assert tool_output(responses.requests[2], "right-lookup") == "right"
            assert tool_output(responses.requests[4], "left-lookup") == "left"
