"""Qualify the optional example against the pinned AG-UI event schema.

Only the model is simulated. The SDK Turn, tools, event retention and cleanup
run through the actual native runtime. Install examples/ag_ui/requirements.txt.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from agents import Agent, NativeError, TurnStoppedError, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    text_response,
)
from tests.test_native_agent_observation import provider

agui = pytest.importorskip("ag_ui.core", reason="optional: examples/ag_ui/requirements.txt")
from examples.ag_ui.stream import stream_turn  # noqa: E402

PARSER = TypeAdapter(agui.Event)


@pytest.mark.parametrize("mode", ["prompted", "native"])
async def test_fully_observed_decoded_string_uses_selected_output_mode(
    responses: ResponsesFixture, mode: str
) -> None:
    from agents import NativeOutput, PromptedOutput
    from agents.observation import EventGap, OutputTextDelta

    selected = PromptedOutput(str) if mode == "prompted" else NativeOutput(str)
    raw_text = '"Selected answer"' if mode == "prompted" else '{"response":"Selected answer"}'
    responses.respond(*text_response("msg_selected", raw_text))
    async with (
        timeout(20),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        turn = await session.submit("Return a value", output_type=selected)
        result = await turn.get_result()
        assert result.final_output == "Selected answer" and result.turn is turn
        observed = [event async for event in turn.events()]
        assert not any(isinstance(event, EventGap) for event in observed)
        assert (
            "".join(event.delta for event in observed if isinstance(event, OutputTextDelta))
            == raw_text
        )
        frames = await read_body(stream_turn(turn, session=session, output_type=selected))
        assert isinstance(frames[-1], agui.RunFinishedEvent)
        assert frames[-1].run_id == turn.submission_id
        assert await turn.get_result() is result
        assert len(responses.requests) == 1


def read_frame(frame: str) -> Any:
    # The official EventEncoder emits one SSE data field. Extract that field;
    # parse the union with the package's own discriminator/schema, not ours.
    assert frame.startswith("data: ") and frame.endswith("\n\n")
    assert frame.count("\n\n") == 1 and frame.count("\n") == 2
    return PARSER.validate_json(frame[6:-2])


async def read_body(body: AsyncIterator[str]) -> list[Any]:
    return [read_frame(frame) async for frame in body]


async def test_actual_protocol_parses_real_ids_text_and_redacted_progress(
    responses: ResponsesFixture,
) -> None:
    @function_tool
    async def lookup(secret: str) -> str:
        """Read a record."""
        assert secret == "synthetic-private-argument"
        return "synthetic-private-result"

    responses.respond(
        *text_response("msg_check", "Checking\n", "", "雪"),
        function_call("lookup", {"secret": "synthetic-private-argument"}, "lookup_42"),
    )
    responses.respond(*text_response("msg_answer", "Found ", "it."))
    responses.respond(*text_response("msg_sibling", "other turn"))
    async with (
        timeout(20),
        Agent(
            instructions="Read.", tools=[lookup], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("lookup")
        frames = await read_body(stream_turn(turn, session=session, output_type=None))
        assert (await turn.get_result()).final_output == "Found it."
        later = await session.submit("another turn")
        assert (await later.get_result()).final_output == "other turn"
        assert isinstance(frames[0], agui.RunStartedEvent)
        assert isinstance(frames[-1], agui.RunFinishedEvent)
        assert frames[0].thread_id == frames[-1].thread_id == session.id
        assert frames[0].run_id == frames[-1].run_id == turn.submission_id
        assert frames[0].run_id != later.submission_id
        messages = [
            f
            for f in frames
            if isinstance(
                f,
                (
                    agui.TextMessageStartEvent,
                    agui.TextMessageContentEvent,
                    agui.TextMessageEndEvent,
                ),
            )
        ]
        assert [(f.type.value, f.message_id) for f in messages] == [
            ("TEXT_MESSAGE_START", "msg_check"),
            ("TEXT_MESSAGE_CONTENT", "msg_check"),
            ("TEXT_MESSAGE_CONTENT", "msg_check"),
            ("TEXT_MESSAGE_START", "msg_answer"),
            ("TEXT_MESSAGE_CONTENT", "msg_answer"),
            ("TEXT_MESSAGE_CONTENT", "msg_answer"),
            ("TEXT_MESSAGE_END", "msg_check"),
            ("TEXT_MESSAGE_END", "msg_answer"),
        ]
        assert [f.delta for f in messages if isinstance(f, agui.TextMessageContentEvent)] == [
            "Checking\n",
            "雪",
            "Found ",
            "it.",
        ]
        progress = [f for f in frames if isinstance(f, agui.CustomEvent)]
        assert [f.name for f in progress] == ["sdk.tool_progress", "sdk.tool_progress"]
        assert [f.value["type"] for f in progress] == ["tool_started", "tool_finished"]
        assert all(
            f.value["call_id"] == "lookup_42" and f.value["name"] == "lookup" for f in progress
        )
        assert progress[-1].value["status"] == "completed"
        assert "synthetic-private" not in repr(frames)
        assert "msg_sibling" not in repr(frames)
        # Examples of wire shapes that a look-alike serializer might accept
        # but the official pinned schema must reject.
        for invalid in (
            '{"type":"text-delta","id":"msg_check","delta":"x"}',
            '{"type":"TEXT_MESSAGE_CONTENT","messageId":"msg_check","delta":""}',
            '{"type":"RUN_ERROR","message":"stopped","runId":"made-up"}',
        ):
            with pytest.raises(ValidationError):
                PARSER.validate_json(invalid)


async def test_failed_tool_keeps_details_private_and_never_claims_success(
    responses: ResponsesFixture,
) -> None:
    @function_tool
    async def fail() -> str:
        """Fail the invocation."""
        raise ValueError("private synthetic callback detail")

    responses.respond(function_call("fail", {}, "failed-call"))
    async with (
        timeout(15),
        Agent(
            instructions="Call fail.", tools=[fail], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("fail")
        frames = await read_body(stream_turn(turn, session=session, output_type=None))
        with pytest.raises(ValueError, match="private synthetic"):
            await turn.get_result()
        assert isinstance(frames[-1], agui.RunErrorEvent)
        assert frames[-1].code == "turn_failed"
        assert not any(isinstance(f, agui.RunFinishedEvent) for f in frames)
        assert "private synthetic" not in repr(frames)


async def test_forwarded_chained_stop_uses_neutral_outcome_text(
    responses: ResponsesFixture,
) -> None:
    entered = asyncio.Event()

    @function_tool
    async def hold() -> str:
        """Wait for the separate Turn's owner to stop it."""
        entered.set()
        await asyncio.Future()
        return "unused"

    responses.respond(function_call("hold", {}, "separate-stop"))
    responses.respond(function_call("forward_stop", {}, "forwarded-stop"))
    async with (
        timeout(20),
        Agent(
            instructions="Call hold.", tools=[hold], provider=provider(responses)
        ).session() as other_session,
    ):
        stopped = await other_session.submit("hold")
        await entered.wait()
        await stopped.cancel()
        with pytest.raises(TurnStoppedError) as original:
            await stopped.get_result()
        assert isinstance(original.value.__cause__, asyncio.CancelledError)

        @function_tool
        async def forward_stop() -> str:
            """Forward the separate Turn's settled outcome unchanged."""
            return (await stopped.get_result()).final_output

        async with Agent(
            instructions="Call forward_stop.",
            tools=[forward_stop],
            provider=provider(responses),
        ).session() as session:
            turn = await session.submit("forward_stop")
            frames = await read_body(stream_turn(turn, session=session, output_type=None))
            with pytest.raises(TurnStoppedError) as forwarded:
                await turn.get_result()
            assert forwarded.value is original.value
            assert isinstance(frames[-1], agui.RunErrorEvent)
            assert frames[-1].code == "turn_cancelled"
            assert frames[-1].message == "The turn ended with a stop-related error."
            assert "owner stopped" not in repr(frames)
            assert not any(isinstance(frame, agui.RunFinishedEvent) for frame in frames)


async def test_confirmed_stop_reports_error_only_after_callback_join(
    responses: ResponsesFixture,
) -> None:
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @function_tool
    async def hold() -> str:
        """Wait until the owner stops this work."""
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()
        return "unused"

    responses.respond(function_call("hold", {}, "owned-call"))
    responses.respond(assistant_output("next turn survives"))
    async with (
        timeout(15),
        Agent(
            instructions="Call hold.", tools=[hold], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("hold")
        await entered.wait()
        frames: list[Any] = []

        async def display() -> None:
            async for frame in stream_turn(turn, session=session, output_type=None):
                frames.append(read_frame(frame))

        viewer = asyncio.create_task(display())
        stopping = asyncio.create_task(turn.cancel())
        try:
            await cleaning.wait()
            assert not viewer.done()
            assert not any(
                isinstance(f, (agui.RunErrorEvent, agui.RunFinishedEvent)) for f in frames
            )
            release.set()
            await stopping
            await viewer
            assert isinstance(frames[-1], agui.RunErrorEvent)
            assert frames[-1].code == "turn_cancelled"
            with pytest.raises(TurnStoppedError):
                await turn.get_result()
            later = await session.submit("next")
            await turn.cancel()
            assert (await later.get_result()).final_output == "next turn survives"
        finally:
            release.set()
            await asyncio.gather(stopping, viewer, return_exceptions=True)


@pytest.mark.parametrize("disconnect", ["close", "cancel_reader"])
async def test_disconnecting_a_passive_reader_does_not_stop_its_turn(
    responses: ResponsesFixture,
    disconnect: str,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    @function_tool
    async def hold() -> str:
        """Keep the Turn live while its observer disconnects."""
        calls.append("entered")
        entered.set()
        await release.wait()
        calls.append("completed")
        return "held result"

    responses.respond(function_call("hold", {}, "live-call"))
    responses.respond(*text_response("msg_survives", "work ", "survives"))
    async with (
        timeout(15),
        Agent(
            instructions="Call hold.", tools=[hold], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("hold")
        try:
            await entered.wait()
            body = stream_turn(turn, session=session, output_type=None)
            assert isinstance(read_frame(await anext(body)), agui.RunStartedEvent)
            progress = read_frame(await anext(body))
            assert (
                isinstance(progress, agui.CustomEvent) and progress.value["call_id"] == "live-call"
            )
            if disconnect == "close":
                await body.aclose()
            else:
                reading = asyncio.create_task(anext(body))
                await asyncio.sleep(0)
                assert not reading.done()
                reading.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await reading
            with pytest.raises(StopAsyncIteration):
                await anext(body)
            assert calls == ["entered"]
            release.set()
            assert (await turn.get_result()).final_output == "work survives"
            assert calls == ["entered", "completed"]
        finally:
            release.set()


async def test_lost_turns_resync_but_a_fully_retained_later_turn_remains_viewable(
    responses: ResponsesFixture,
) -> None:
    responses.respond(*text_response("msg_old", "old"))
    responses.respond(*text_response("msg_flood", *(["x"] * 350)))
    responses.respond(*text_response("msg_current", "fully ", "retained"))
    async with (
        timeout(20),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        old = await session.submit("old")
        assert (await old.get_result()).final_output == "old"
        flood = await session.submit("flood")
        assert (await flood.get_result()).final_output == "x" * 350
        for lost in (old, flood):
            frames = await read_body(stream_turn(lost, session=session, output_type=None))
            assert len(frames) == 2
            assert isinstance(frames[0], agui.RunStartedEvent)
            assert frames[0].run_id == lost.submission_id
            assert isinstance(frames[1], agui.RunErrorEvent)
            assert frames[1].code == "resync_required"
        assert "x" * 350 in repr([item async for item in session.items()])
        current = await session.submit("current")
        frames = await read_body(stream_turn(current, session=session, output_type=None))
        assert (await current.get_result()).final_output == "fully retained"
        assert isinstance(frames[-1], agui.RunFinishedEvent)
        assert frames[-1].run_id == current.submission_id
        assert [f.delta for f in frames if isinstance(f, agui.TextMessageContentEvent)] == [
            "fully ",
            "retained",
        ]


async def test_gap_in_an_open_view_closes_shown_text_without_inventing_missing_content(
    responses: ResponsesFixture,
) -> None:
    release = asyncio.Event()
    oversized = "z" * 70_000

    async def hold_large_delta(event: dict[str, Any]) -> None:
        if event["type"] == "response.output_text.delta" and event["delta"] == oversized:
            await release.wait()

    responses.before_event = hold_large_delta
    responses.progress_while_before_event = True
    responses.respond(*text_response("msg_live_loss", "seen ", oversized))
    async with (
        timeout(20),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        turn = await session.submit("large")
        try:
            body = stream_turn(turn, session=session, output_type=None)
            assert isinstance(read_frame(await anext(body)), agui.RunStartedEvent)
            start = read_frame(await anext(body))
            assert (
                isinstance(start, agui.TextMessageStartEvent)
                and start.message_id == "msg_live_loss"
            )
            seen = read_frame(await anext(body))
            assert isinstance(seen, agui.TextMessageContentEvent) and seen.delta == "seen "
            release.set()
            assert (await turn.get_result()).final_output == "seen " + oversized
            rest = await read_body(body)
            assert len(rest) == 2
            assert (
                isinstance(rest[0], agui.TextMessageEndEvent)
                and rest[0].message_id == start.message_id
            )
            assert isinstance(rest[1], agui.RunErrorEvent) and rest[1].code == "resync_required"
        finally:
            release.set()


async def test_evicted_root_with_retained_repair_requires_resync(
    responses: ResponsesFixture,
) -> None:
    from pydantic import BaseModel

    from agents import PromptedOutput
    from agents.observation import EventGap, TurnStarted

    class Answer(BaseModel):
        value: int

    # Real repair events fill the bounded suffix exactly, evicting the
    # original start while retaining the repair start and its output.
    responses.respond(*text_response("msg_invalid", "not json"))
    responses.respond(*text_response("msg_repair", *([" "] * 249), '{"value":7}'))
    async with (
        timeout(20),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        selected = PromptedOutput(Answer, max_retries=1)
        turn = await session.submit("Return a value", output_type=selected)
        result = await turn.get_result()
        assert result.final_output == Answer(value=7) and result.turn is turn
        raw = [event async for event in turn.events()]
        assert isinstance(raw[0], EventGap)
        assert isinstance(raw[1], TurnStarted)
        assert raw[1].submission_id != turn.submission_id
        assert raw[1].parent_submission_id == turn.submission_id
        frames = await read_body(stream_turn(turn, session=session, output_type=selected))
        assert len(frames) == 2
        assert isinstance(frames[0], agui.RunStartedEvent)
        assert frames[0].run_id == turn.submission_id
        assert isinstance(frames[1], agui.RunErrorEvent)
        assert frames[1].code == "resync_required"
        # The UI reader never cancels, replays, or changes the owned result.
        assert await turn.get_result() is result
        assert len(responses.requests) == 2


@pytest.mark.parametrize("older_eviction", [False, True], ids=["no-gap", "older-global-gap"])
async def test_queued_cancel_without_native_start_is_not_history_loss(
    responses: ResponsesFixture, older_eviction: bool
) -> None:
    from agents.observation import EventGap

    entered, release = asyncio.Event(), asyncio.Event()

    @function_tool
    async def hold() -> str:
        entered.set()
        await release.wait()
        return "held"

    if older_eviction:
        responses.respond(*text_response("msg_old_flood", *(["x"] * 350)))
    responses.respond(function_call("hold", {}, "hold-first"))
    responses.respond(assistant_output("first survived"))
    responses.respond(assistant_output("later survived"))
    async with (
        timeout(20),
        Agent(
            instructions="Answer.", tools=[hold], provider=provider(responses)
        ).session() as session,
    ):
        if older_eviction:
            old = await session.submit("old")
            assert (await old.get_result()).final_output == "x" * 350
        previous_requests = int(older_eviction)
        first = await session.submit("first")
        try:
            await entered.wait()
            queued = await session.submit("queued", if_busy="queue")
            await queued.cancel()
            assert queued.turn_id is None
            observations = [event async for event in queued.events()]
            if older_eviction:
                assert len(observations) == 1 and isinstance(observations[0], EventGap)
            else:
                assert observations == []
            with pytest.raises(TurnStoppedError):
                await queued.get_result()
            frames = await read_body(stream_turn(queued, session=session, output_type=None))
            assert len(frames) == 2 and isinstance(frames[0], agui.RunStartedEvent)
            assert isinstance(frames[1], agui.RunErrorEvent)
            assert frames[1].code == "turn_cancelled"
            assert len(responses.requests) == previous_requests + 1
        finally:
            release.set()
        assert (await first.get_result()).final_output == "first survived"
        assert len(responses.requests) == previous_requests + 2
        later = await session.submit("later")
        assert (await later.get_result()).final_output == "later survived"
        assert len(responses.requests) == previous_requests + 3


@pytest.mark.parametrize("intro", [False, True], ids=["done-only", "after-commentary"])
async def test_done_only_result_needs_resync_without_fabricated_message(
    responses: ResponsesFixture, intro: bool
) -> None:
    calls: list[str] = []

    @function_tool
    async def lookup() -> str:
        """Read one value."""
        calls.append("lookup")
        return "not UI text"

    if intro:
        responses.respond(
            *text_response("msg_reading", "Checking"),
            function_call("lookup", {}, "lookup"),
        )
    responses.respond(assistant_output("Final answer not present in deltas"))
    responses.respond(*text_response("msg_later", "Later intact"))
    async with (
        timeout(15),
        Agent(
            instructions="Read.", tools=[lookup], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("done-only")
        frames = await read_body(stream_turn(turn, session=session, output_type=None))
        result = await turn.get_result()
        assert result.turn is turn
        assert result.final_output == "Final answer not present in deltas"
        assert calls == (["lookup"] if intro else [])
        assert isinstance(frames[-1], agui.RunErrorEvent)
        assert frames[-1].code == "resync_required"
        assert not any(isinstance(f, agui.RunFinishedEvent) for f in frames)
        assert "Final answer not present in deltas" not in repr(frames)
        messages = [
            f
            for f in frames
            if isinstance(
                f,
                (
                    agui.TextMessageStartEvent,
                    agui.TextMessageContentEvent,
                    agui.TextMessageEndEvent,
                ),
            )
        ]
        assert all(f.message_id == "msg_reading" for f in messages)
        later = await session.submit("next")
        next_frames = await read_body(stream_turn(later, session=session, output_type=None))
        assert (await later.get_result()).final_output == "Later intact"
        assert isinstance(next_frames[-1], agui.RunFinishedEvent)


@pytest.mark.parametrize(
    ("intro", "answer", "observed", "finish"),
    [
        (True, "", False, False),
        (True, "Checking", False, False),
        (True, "Checking", True, True),
        (False, "", False, True),
    ],
    ids=["blank-after-commentary", "done-only-equal", "observed-equal", "blank-alone"],
)
async def test_final_text_must_be_observed_after_tool_progress(
    responses: ResponsesFixture, intro: bool, answer: str, observed: bool, finish: bool
) -> None:
    @function_tool
    async def lookup() -> str:
        """Read one value."""
        return "not UI text"

    if intro:
        responses.respond(
            *text_response("msg_comment", "Checking"),
            function_call("lookup", {}, "lookup"),
        )
    if observed:
        responses.respond(*text_response("msg_final", answer))
    else:
        responses.respond(assistant_output(answer))
    async with (
        timeout(15),
        Agent(
            instructions="Read.", tools=[lookup], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("read")
        frames = await read_body(stream_turn(turn, session=session, output_type=None))
        assert (await turn.get_result()).final_output == answer
        assert len(responses.requests) == (2 if intro else 1)
        if finish:
            assert isinstance(frames[-1], agui.RunFinishedEvent)
        else:
            assert isinstance(frames[-1], agui.RunErrorEvent)
            assert frames[-1].code == "resync_required"
        started = [f.message_id for f in frames if isinstance(f, agui.TextMessageStartEvent)]
        ended = [f.message_id for f in frames if isinstance(f, agui.TextMessageEndEvent)]
        assert (
            started
            == ended
            == (["msg_comment"] if intro else []) + (["msg_final"] if observed else [])
        )


@pytest.mark.parametrize("observed", [False, True], ids=["missing-final", "observed-final"])
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Synthetic intermediate commentary", "Synthetic intermediate commentary"),
        (" \t\n ", ""),
        (
            " \x1c\x1d\x1e\x1fanswer\x1c\x1d\x1e\x1f \n",
            " \x1c\x1d\x1e\x1fanswer\x1c\x1d\x1e\x1f \n",
        ),
    ],
    ids=["text", "canonical-empty", "c0-is-content"],
)
async def test_same_response_commentary_cannot_stand_in_for_final_identity(
    responses: ResponsesFixture, observed: bool, text: str, expected: str
) -> None:
    from tests.test_native_response_integrity import full_envelopes, phased_answer

    full_envelopes(responses)
    phased = phased_answer(text)
    completed_text = {
        event["output_index"]: event["item"]["content"][0]["text"]
        for event in phased
        if event["type"] == "response.output_item.done"
    }
    events = []
    for event in phased:
        events.append(event)
        if event["type"] == "response.output_item.added" and (
            event["output_index"] == 0 or observed
        ):
            events.append(
                {
                    "type": "response.output_text.delta",
                    "item_id": event["item"]["id"],
                    "output_index": event["output_index"],
                    "content_index": 0,
                    "delta": completed_text[event["output_index"]],
                }
            )
    responses.respond(*events)
    async with (
        timeout(15),
        Agent(
            model="gpt-5.2", instructions="Answer.", provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("answer")
        frames = await read_body(stream_turn(turn, session=session, output_type=None))
        assert (await turn.get_result()).final_output == expected
        assert len(responses.requests) == 1
        ids = [
            event["item"]["id"] for event in phased if event["type"] == "response.output_item.done"
        ]
        shown = [f.message_id for f in frames if isinstance(f, agui.TextMessageStartEvent)]
        ended = [f.message_id for f in frames if isinstance(f, agui.TextMessageEndEvent)]
        assert shown == ended == (ids if observed else ids[:1])
        if observed:
            assert isinstance(frames[-1], agui.RunFinishedEvent)
        else:
            assert isinstance(frames[-1], agui.RunErrorEvent)
            assert frames[-1].code == "resync_required"


async def test_nonblank_final_requires_exact_displayed_whitespace(
    responses: ResponsesFixture,
) -> None:
    events = text_response("msg_final", " answer ")
    events[-1]["item"]["content"][0]["text"] = "answer"
    responses.respond(*events)
    async with (
        timeout(15),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        turn = await session.submit("answer")
        frames = await read_body(stream_turn(turn, session=session, output_type=None))
        assert (await turn.get_result()).final_output == "answer"
        shown = [f for f in frames if isinstance(f, agui.TextMessageContentEvent)]
        assert [(f.message_id, f.delta) for f in shown] == [("msg_final", " answer ")]
        ended = [f.message_id for f in frames if isinstance(f, agui.TextMessageEndEvent)]
        assert ended == ["msg_final"]
        assert isinstance(frames[-1], agui.RunErrorEvent)
        assert frames[-1].code == "resync_required"
        assert len(responses.requests) == 1


@pytest.mark.parametrize("observed", [False, True], ids=["missing-final", "observed-final"])
@pytest.mark.parametrize(
    ("messages", "effective"),
    [
        ([("final_answer", "earlier"), (None, "later")], 1),
        ([("final_answer", "kept"), (None, " \n\t ")], 0),
        ([("final_answer", "earlier"), (None, "middle"), ("final_answer", "latest")], 2),
        ([("final_answer", "earlier"), (None, "\x1c")], 1),
    ],
    ids=["later-unphased", "blank-tail", "later-final", "rust-nonblank-separator"],
)
async def test_effective_final_item_follows_canonical_order(
    responses: ResponsesFixture,
    messages: list[tuple[str | None, str]],
    effective: int,
    observed: bool,
) -> None:
    from tests.test_native_response_integrity import full_envelopes

    full_envelopes(responses)
    events = []
    ids = []
    for index, (phase, text) in enumerate(messages):
        done = assistant_output(text)
        done["item"].update(status="completed", phase=phase)
        ids.append(done["item"]["id"])
        events.append(
            {
                "type": "response.output_item.added",
                "output_index": index,
                "item": {**done["item"], "status": "in_progress", "content": []},
            }
        )
        if index != effective or observed:
            events.append(
                {
                    "type": "response.output_text.delta",
                    "item_id": done["item"]["id"],
                    "output_index": index,
                    "content_index": 0,
                    "delta": text,
                }
            )
        events.append({**done, "output_index": index})
    responses.respond(*events)
    async with (
        timeout(20),
        Agent(
            model="gpt-5.2", instructions="Answer.", provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("answer")
        result = await turn.get_result()
        assert result.final_output == messages[effective][1]
        recorded = [
            item
            async for item in session.items()
            if item["type"] == "message"
            and item["role"] == "assistant"
            and item["turn_id"] == turn.turn_id
        ]
        assert [
            (item["phase"], "".join(part["text"] for part in item["content"])) for item in recorded
        ] == messages
        frames = await read_body(stream_turn(turn, session=session, output_type=None))
        shown = [f.message_id for f in frames if isinstance(f, agui.TextMessageStartEvent)]
        ended = [f.message_id for f in frames if isinstance(f, agui.TextMessageEndEvent)]
        assert (
            shown
            == ended
            == [item_id for i, item_id in enumerate(ids) if i != effective or observed]
        )
        if observed:
            assert isinstance(frames[-1], agui.RunFinishedEvent)
        else:
            assert isinstance(frames[-1], agui.RunErrorEvent)
            assert frames[-1].code == "resync_required"
        assert await turn.get_result() is result
        assert len(responses.requests) == 1


@pytest.mark.parametrize("earlier", ["missing", "different", "valid", "malformed"])
async def test_every_displayed_part_must_match_canonical_history(
    responses: ResponsesFixture, earlier: str
) -> None:
    initial = text_response("msg_superseded", "earlier displayed text")
    if earlier == "missing":
        initial = initial[:2]
    elif earlier == "malformed":
        initial = initial[:2] + [
            {
                "type": "response.output_item.done",
                "item": {"type": "message", "role": "assistant", "id": "broken"},
            }
        ]
    elif earlier == "different":
        initial[-1]["item"]["content"][0]["text"] = "canonical earlier text"
    responses.respond(*initial, *text_response("msg_final", "authoritative answer"))
    async with (
        timeout(15),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        turn = await session.submit("answer")
        frames = await read_body(stream_turn(turn, session=session, output_type=None))
        if earlier == "malformed":
            error = "Native model response ended without complete supported output"
            with pytest.raises(NativeError, match=error) as caught:
                await turn.get_result()
            assert str(caught.value) == error
            assert [f.delta for f in frames if isinstance(f, agui.TextMessageContentEvent)] == [
                "earlier displayed text"
            ]
            assert [f.message_id for f in frames if isinstance(f, agui.TextMessageStartEvent)] == [
                "msg_superseded"
            ]
            assert [f.message_id for f in frames if isinstance(f, agui.TextMessageEndEvent)] == [
                "msg_superseded"
            ]
            assert isinstance(frames[-1], agui.RunErrorEvent)
            assert frames[-1].code == "turn_failed"
            assert frames[-1].message == "The turn failed."
            assert not any(isinstance(frame, agui.RunFinishedEvent) for frame in frames)
            assert len(responses.requests) == 1
            return
        assert (await turn.get_result()).final_output == "authoritative answer"
        canonical = {
            item["id"]: "".join(part["text"] for part in item["content"])
            async for item in session.items()
            if item["type"] == "message" and item["role"] == "assistant"
        }
        assert canonical["msg_final"] == "authoritative answer"
        assert (
            canonical.get("msg_superseded")
            == {
                "missing": None,
                "different": "canonical earlier text",
                "valid": "earlier displayed text",
            }[earlier]
        )
        assert [f.delta for f in frames if isinstance(f, agui.TextMessageContentEvent)] == [
            "earlier displayed text",
            "authoritative answer",
        ]
        if earlier == "valid":
            assert isinstance(frames[-1], agui.RunFinishedEvent)
        else:
            assert isinstance(frames[-1], agui.RunErrorEvent)
            assert frames[-1].code == "resync_required"


async def test_child_cancel_uses_settled_native_outcome_and_preserves_live_sibling(
    tmp_path, monkeypatch
) -> None:
    from aiohttp.test_utils import TestServer

    from agents import FunctionTool, InteractionExpiredError, NativeError
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import trust
    from tests.test_native_child_manual import ManualChildren, declaration_for
    from tests.test_native_executor_tools import declaration
    from tests.test_native_manual_tools import LookupArgs, next_request

    peer = ManualChildren()
    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    async with (
        timeout(30),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        async with declaration_for(peer, executor, lookup).session(
            environment=declaration(executor)
        ) as session:
            await (await session.submit("Start both children")).get_result()
            requests = session.interactions()
            first = await next_request(requests, lookup)
            second = await next_request(requests, lookup)
            by_name = {request.arguments.key: request for request in (first, second)}
            children = {child.id: child async for child in session.subagents()}
            alpha = children[by_name["alpha"].subagent_id]
            beta = children[by_name["beta"].subagent_id]
            assert alpha.turn is not None and beta.turn is not None
            assert await alpha.try_cancel(alpha.turn)
            frames = await read_body(stream_turn(alpha.turn, session=session, output_type=None))
            with pytest.raises(NativeError):
                await alpha.turn.get_result()
            with pytest.raises(InteractionExpiredError):
                await by_name["alpha"].respond("late")
            assert frames[0].thread_id == alpha.id
            assert frames[0].run_id == alpha.turn.submission_id
            assert isinstance(frames[-1], agui.RunErrorEvent)
            assert frames[-1].code == "turn_cancelled"
            assert not any(isinstance(frame, agui.RunFinishedEvent) for frame in frames)
            await by_name["beta"].respond("answer-beta")
            assert (await beta.turn.get_result()).final_output == "beta complete"
            assert len(peer.child_requests["alpha"]) == 1
            assert len(peer.child_requests["beta"]) == 2
            await requests.aclose()
