"""Test the copyable UI example with the public SDK and native Responses fixture.

Frame expectations check this recipe, not conformance against an AI SDK client.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agents import Agent, NativeError, TurnStoppedError, function_tool
from agents.observation import EventGap, OutputTextDelta, TurnStarted
from examples.codex.vercel_stream import export_metadata, passive_wait, stream_turn, wait_owned_turn
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    text_response,
)
from tests.test_native_agent import provider
from tests.test_review_ii_steering_tail import ProjectContinuation

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/vercel_ui_v1.json").read_text(encoding="utf-8")
)


def read_frame(frame: bytes) -> Any:
    # This parses the documented SSE boundary; JSON must not create extra
    # lines/events even when model text contains a newline.
    assert frame.startswith(b"data: ") and frame.endswith(b"\n\n")
    assert frame.count(b"\n\n") == 1
    data = frame[6:-2].decode("utf-8")
    return data if data == "[DONE]" else json.loads(data)


async def read_body(body: AsyncIterator[bytes]) -> list[Any]:
    return [read_frame(frame) async for frame in body]


async def test_real_ids_and_exact_success_with_custom_progress_only(
    responses: ResponsesFixture,
) -> None:
    @function_tool
    async def lookup(secret: str) -> str:
        """Read a record."""
        assert secret == "synthetic-argument-not-ui-content"
        return "synthetic-result-not-ui-content"

    responses.respond(
        *text_response("msg_intro_r10", "Checking\n", "雪"),
        function_call("lookup", {"secret": "synthetic-argument-not-ui-content"}, "call_r10"),
    )
    responses.respond(*text_response("msg_final_r10", "Found ", "it."))
    responses.respond(*text_response("msg_other", "unrelated"))
    async with (
        timeout(20),
        Agent(
            instructions="Read.", tools=[lookup], provider=provider(responses)
        ).session() as session,
    ):
        first = await session.submit("lookup")
        frames = await read_body(stream_turn(first, session=session))
        assert (await first.get_result()).final_output == "Found it."
        second = await session.submit("different turn")
        assert (await second.get_result()).final_output == "unrelated"

        # The native reader and callback can publish in either order. Vercel
        # requires ordered text parts and ordered tool progress, not a made-up
        # total order between the two producers.
        def text_parts(items):
            return [
                item for item in items if item == "[DONE]" or item["type"] != "data-tool-progress"
            ]

        def progress_parts(items):
            return [
                item for item in items if item != "[DONE]" and item["type"] == "data-tool-progress"
            ]

        assert text_parts(frames) == text_parts(FIXTURE["success"])
        assert progress_parts(frames) == progress_parts(FIXTURE["success"])
        assert await read_body(stream_turn(first, session=session)) == frames
        # A Session cursor cannot be used to resume a UI message.
        with pytest.raises(TypeError, match="after"):
            stream_turn(first, session=session, after=1)  # type: ignore[call-arg]
        assert await read_body(stream_turn(first, session=session)) == frames


async def test_owned_steering_continuation_requires_resync_without_stopping_turn(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses.respond(*text_response("msg_before_steer", "earlier answer"))
    responses.respond(*text_response("msg_after_steer", "successor answer"))
    responses.respond(*text_response("msg_afterward", "later turn"))
    async with (
        timeout(20),
        Agent(instructions="Read.", provider=provider(responses)).session() as session,
    ):
        # Only project the unschedulable Core steering-tail receipt and conditional
        # admission, as in the native queue tests. Both turns and their output
        # item identities still come from the real engine.
        monkeypatch.setattr(session, "_native", ProjectContinuation(session._native))
        turn = await session.submit("first")
        frames = await read_body(stream_turn(turn, session=session))
        result = await turn.get_result()
        assert result.final_output == "successor answer"
        starts = [event async for event in turn.events() if isinstance(event, TurnStarted)]
        assert len(starts) == 2 and starts[0].turn_id != starts[1].turn_id
        assert frames[-2:] == [{"type": "error", "errorText": "resync_required"}, "[DONE]"]
        assert {"type": "text-end", "id": "msg_before_steer"} in frames
        assert not any(
            frame == {"type": "finish"} or frame == {"type": "abort"} for frame in frames
        )
        assert "successor answer" not in json.dumps(frames)
        assert (await turn.get_result()) is result
        later = await session.submit("next")
        assert (await later.get_result()).final_output == "later turn"
        later_frames = await read_body(stream_turn(later, session=session))
        assert later_frames[-2:] == [{"type": "finish"}, "[DONE]"]


async def test_failure_never_exports_tool_exception_or_finishes(
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
        body = await read_body(stream_turn(turn, session=session))
        with pytest.raises(ValueError, match="private synthetic"):
            await turn.get_result()
        assert body[-2:] == [{"type": "error", "errorText": "turn_failed"}, "[DONE]"]
        assert "private synthetic" not in json.dumps(body)
        assert not any(item == {"type": "finish"} or item == {"type": "abort"} for item in body)


async def test_confirmed_stop_is_abort_only_after_join(responses: ResponsesFixture) -> None:
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
        observed: list[Any] = []

        async def display() -> None:
            async for frame in stream_turn(turn, session=session):
                observed.append(read_frame(frame))

        viewer = asyncio.create_task(display())
        stopping = asyncio.create_task(turn.cancel())
        try:
            await cleaning.wait()
            assert not viewer.done()
            assert {"type": "abort"} not in observed
            assert {"type": "finish"} not in observed
            release.set()
            await stopping
            await viewer
            assert observed[-2:] == [{"type": "abort"}, "[DONE]"]
            with pytest.raises(TurnStoppedError):
                await turn.get_result()
            later = await session.submit("next")
            await turn.cancel()
            assert (await later.get_result()).final_output == "next turn survives"
        finally:
            release.set()
            await asyncio.gather(stopping, viewer, return_exceptions=True)


@pytest.mark.parametrize("disconnect", ["close", "cancel_reader"])
async def test_reader_disconnect_releases_only_the_reader(
    responses: ResponsesFixture, disconnect: str
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    @function_tool
    async def hold() -> str:
        """Keep the Turn live while its observer disconnects."""
        entered.set()
        await release.wait()
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
        await entered.wait()
        stream = stream_turn(turn, session=session)
        assert read_frame(await anext(stream)) == {"type": "start"}
        assert read_frame(await anext(stream))["data"]["call_id"] == "live-call"
        if disconnect == "close":
            await stream.aclose()
        else:
            reading = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            assert not reading.done()
            reading.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reading
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        release.set()
        assert (await turn.get_result()).final_output == "work survives"
        assert (await read_body(stream_turn(turn, session=session)))[-2:] == [
            {"type": "finish"},
            "[DONE]",
        ]


async def test_gap_requires_resync_not_success(
    responses: ResponsesFixture,
) -> None:
    responses.respond(*text_response("msg_before_gap", "old"))
    responses.respond(*text_response("msg_flood", *(["x"] * 350)))
    async with (
        timeout(20),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        old = await session.submit("old")
        assert (await old.get_result()).final_output == "old"
        flood = await session.submit("flood")
        assert (await flood.get_result()).final_output == "x" * 350
        assert await read_body(stream_turn(old, session=session)) == FIXTURE["lost"]
        assert await read_body(stream_turn(flood, session=session)) == FIXTURE["lost"]
        # Resync uses authoritative history chosen by the application.
        history = [item async for item in session.items()]
        assert "x" * 350 in repr(history)


async def test_retained_turn_streams_after_earlier_turn_loss(responses: ResponsesFixture) -> None:
    responses.respond(*text_response("msg_old_loss", *(["a"] * 350)))
    responses.respond(*text_response("msg_retained", "fully ", "retained"))
    async with (
        timeout(20),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        old = await session.submit("old")
        assert (await old.get_result()).final_output == "a" * 350
        assert await read_body(stream_turn(old, session=session)) == FIXTURE["lost"]

        # Open the new view in the same Session, without an application cursor.
        current = await session.submit("current")
        expected = [
            {"type": "start"},
            {"type": "text-start", "id": "msg_retained"},
            {"type": "text-delta", "id": "msg_retained", "delta": "fully "},
            {"type": "text-delta", "id": "msg_retained", "delta": "retained"},
            {"type": "text-end", "id": "msg_retained"},
            {"type": "finish"},
            "[DONE]",
        ]
        assert await read_body(stream_turn(current, session=session)) == expected
        assert (await current.get_result()).final_output == "fully retained"
        assert await read_body(stream_turn(current, session=session)) == expected
        assert await read_body(stream_turn(old, session=session)) == FIXTURE["lost"]

        # Raw observations retain their public Session-global gap contract.
        raw = [event async for event in current.events()]
        assert isinstance(raw[0], EventGap)
        assert isinstance(raw[1], TurnStarted)
        assert raw[0].sequence < raw[1].sequence
        assert [event.delta for event in raw if isinstance(event, OutputTextDelta)] == [
            "fully ",
            "retained",
        ]


async def test_open_reader_current_turn_loss_still_requires_resync(
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
        turn = await session.submit("slow reader")
        body = stream_turn(turn, session=session)
        try:
            frames = [read_frame(await anext(body)) for _ in range(3)]
            assert frames == [
                {"type": "start"},
                {"type": "text-start", "id": "msg_live_loss"},
                {"type": "text-delta", "id": "msg_live_loss", "delta": "seen "},
            ]
            release.set()
            assert (await turn.get_result()).final_output == "seen " + oversized
            frames.extend(await read_body(body))
            assert frames[-2:] == [
                {"type": "error", "errorText": "resync_required"},
                "[DONE]",
            ]
            assert {"type": "finish"} not in frames
            assert {"type": "abort"} not in frames
            assert oversized not in json.dumps(frames)
            # TextUIPart stays streaming until text-end, including on error.
            # https://github.com/vercel/ai/blob/main/packages/ai/src/ui/process-ui-message-stream.ts
            streaming: set[str] = set()
            for frame in frames:
                if frame == "[DONE]":
                    break
                if frame["type"] == "text-start":
                    assert frame["id"] not in streaming
                    streaming.add(frame["id"])
                elif frame["type"] == "text-end":
                    streaming.remove(frame["id"])
                elif frame["type"] == "error":
                    assert not streaming, f"UI parts still streaming after resync: {streaming}"
        finally:
            release.set()
            await body.aclose()


async def test_metadata_and_timeout_recipes_keep_ownership_explicit(
    responses: ResponsesFixture,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    @function_tool
    async def hold(secret: str) -> str:
        """Wait for the application."""
        entered.set()
        await release.wait()
        return secret

    responses.respond(function_call("hold", {"secret": "synthetic-hidden-content"}, "recipe-call"))
    responses.respond(*text_response("msg_owned", "display only"))
    responses.respond(function_call("hold", {"secret": "synthetic-hidden-content"}, "stop-call"))
    async with (
        timeout(20),
        Agent(
            instructions="Hold.", tools=[hold], provider=provider(responses)
        ).session() as session,
    ):
        rows = []

        async def sink(row) -> None:
            rows.append(row)

        logger = asyncio.create_task(export_metadata(session, sink))
        try:
            turn = await session.submit("wait")
            await entered.wait()
            with pytest.raises(asyncio.TimeoutError):
                await passive_wait(turn, 0)
            release.set()
            assert (await turn.get_result()).final_output == "display only"
            release.clear()
            entered.clear()
            owned = await session.submit("stop")
            await entered.wait()
            with pytest.raises(asyncio.TimeoutError):
                await wait_owned_turn(owned, 0)
            with pytest.raises(TurnStoppedError):
                await owned.get_result()
            assert "synthetic-hidden-content" not in repr(rows)
            assert "display only" not in repr(rows)
            assert all(
                set(row) <= {"session_id", "sequence", "type", "first_lost_sequence"}
                for row in rows
            )
        finally:
            logger.cancel()
            await asyncio.gather(logger, return_exceptions=True)


async def test_evicted_root_with_retained_repair_requires_resync(
    responses: ResponsesFixture,
) -> None:
    from pydantic import BaseModel

    from agents import PromptedOutput
    from agents.observation import EventGap, TurnStarted

    class Answer(BaseModel):
        value: int

    # Real repair events fill the bounded suffix exactly, evicting the
    # original start while retaining the repair start and its output, including
    # the repair request dispatch and response link.
    responses.respond(*text_response("msg_invalid", "not json"))
    responses.respond(*text_response("msg_repair", *([" "] * 249), '{"value":7}'))
    async with (
        timeout(20),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        turn = await session.submit(
            "Return a value", output_type=PromptedOutput(Answer, max_retries=1)
        )
        result = await turn.get_result()
        assert result.final_output == Answer(value=7) and result.turn is turn
        raw = [event async for event in turn.events()]
        assert isinstance(raw[0], EventGap)
        assert isinstance(raw[1], TurnStarted)
        assert raw[1].submission_id != turn.submission_id
        assert raw[1].parent_submission_id == turn.submission_id
        assert await read_body(stream_turn(turn, session=session)) == FIXTURE["lost"]
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
            assert await read_body(stream_turn(queued, session=session)) == [
                {"type": "start"},
                {"type": "abort"},
                "[DONE]",
            ]
            assert len(responses.requests) == previous_requests + 1
        finally:
            release.set()
        assert (await first.get_result()).final_output == "first survived"
        assert len(responses.requests) == previous_requests + 2
        later = await session.submit("later")
        assert (await later.get_result()).final_output == "later survived"
        assert len(responses.requests) == previous_requests + 3


@pytest.mark.parametrize("mode", ["native", "prompted", "prompted-string"])
async def test_response_body_rejects_non_plain_output_before_open(
    responses: ResponsesFixture, mode: str
) -> None:
    from pydantic import BaseModel

    from agents import PromptedOutput
    from examples.codex.vercel_stream import response_body

    class Answer(BaseModel):
        value: int

    output = (
        Answer
        if mode == "native"
        else PromptedOutput(Answer, max_retries=1)
        if mode == "prompted"
        else PromptedOutput(str, max_retries=1)
    )
    agent = Agent(instructions="Answer.", provider=provider(responses), output_type=output)
    responses.respond(*text_response("invalid", "not json"))
    responses.respond(*text_response("repair", '{"value":7}'))
    with pytest.raises(TypeError, match="plain-text"):
        await read_body(response_body(agent, "Return a value"))
    assert not responses.requests


@pytest.mark.parametrize("output", [None, str], ids=["default", "explicit-str"])
async def test_response_body_accepts_plain_text_output(
    responses: ResponsesFixture, output: type[str] | None
) -> None:
    from examples.codex.vercel_stream import response_body

    responses.respond(*text_response("plain-item", "plain text"))
    frames = await read_body(
        response_body(
            Agent(instructions="Answer.", provider=provider(responses), output_type=output),
            "Say it",
        )
    )
    assert frames == [
        {"type": "start"},
        {"type": "text-start", "id": "plain-item"},
        {"type": "text-delta", "id": "plain-item", "delta": "plain text"},
        {"type": "text-end", "id": "plain-item"},
        {"type": "finish"},
        "[DONE]",
    ]
    assert len(responses.requests) == 1


@pytest.mark.parametrize("intro", [False, True], ids=["done-only", "after-commentary"])
async def test_done_only_text_cannot_finish_without_observed_native_item(
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
        frames = await read_body(stream_turn(turn, session=session))
        result = await turn.get_result()
        assert result.turn is turn
        assert result.final_output == "Final answer not present in deltas"
        assert calls == (["lookup"] if intro else [])
        assert not any(f != "[DONE]" and f["type"] == "finish" for f in frames)
        assert frames[-2:] == [{"type": "error", "errorText": "resync_required"}, "[DONE]"]
        assert "Final answer not present in deltas" not in repr(frames)
        assert all(
            f["id"] == "msg_reading"
            for f in frames
            if isinstance(f, dict) and f["type"].startswith("text-")
        )
        later = await session.submit("next")
        next_frames = await read_body(stream_turn(later, session=session))
        assert (await later.get_result()).final_output == "Later intact"
        assert next_frames[-2:] == [{"type": "finish"}, "[DONE]"]


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
        frames = await read_body(stream_turn(turn, session=session))
        assert (await turn.get_result()).final_output == answer
        assert len(responses.requests) == (2 if intro else 1)
        terminal = (
            {"type": "finish"} if finish else {"type": "error", "errorText": "resync_required"}
        )
        assert frames[-2:] == [terminal, "[DONE]"]
        started = [f["id"] for f in frames if isinstance(f, dict) and f["type"] == "text-start"]
        ended = [f["id"] for f in frames if isinstance(f, dict) and f["type"] == "text-end"]
        assert (
            started
            == ended
            == (["msg_comment"] if intro else []) + (["msg_final"] if observed else [])
        )


@pytest.mark.parametrize("observed", [False, True], ids=["missing-final", "observed-final"])
async def test_same_response_commentary_cannot_stand_in_for_final_identity(
    responses: ResponsesFixture, observed: bool
) -> None:
    from tests.test_native_response_integrity import full_envelopes, phased_answer

    full_envelopes(responses)
    text = "Synthetic intermediate commentary"
    phased = phased_answer(text)
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
                    "delta": text,
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
        frames = await read_body(stream_turn(turn, session=session))
        assert (await turn.get_result()).final_output == text
        assert len(responses.requests) == 1
        ids = [
            event["item"]["id"] for event in phased if event["type"] == "response.output_item.done"
        ]
        shown = [f["id"] for f in frames if isinstance(f, dict) and f["type"] == "text-start"]
        ended = [f["id"] for f in frames if isinstance(f, dict) and f["type"] == "text-end"]
        assert shown == ended == (ids if observed else ids[:1])
        terminal = (
            {"type": "finish"} if observed else {"type": "error", "errorText": "resync_required"}
        )
        assert frames[-2:] == [terminal, "[DONE]"]


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
        frames = await read_body(stream_turn(turn, session=session))
        shown = [f["id"] for f in frames if isinstance(f, dict) and f["type"] == "text-start"]
        ended = [f["id"] for f in frames if isinstance(f, dict) and f["type"] == "text-end"]
        assert (
            shown
            == ended
            == [item_id for i, item_id in enumerate(ids) if i != effective or observed]
        )
        terminal = (
            {"type": "finish"} if observed else {"type": "error", "errorText": "resync_required"}
        )
        assert frames[-2:] == [terminal, "[DONE]"]
        assert await turn.get_result() is result
        assert len(responses.requests) == 1


@pytest.mark.parametrize("earlier", ["missing", "different", "valid"])
async def test_every_displayed_part_must_match_canonical_history(
    responses: ResponsesFixture, earlier: str
) -> None:
    initial = text_response("msg_superseded", "earlier displayed text")
    if earlier == "missing":
        initial = initial[:2] + [
            {
                "type": "response.output_item.done",
                "item": {"type": "message", "role": "assistant", "id": "broken"},
            }
        ]
    elif earlier == "different":
        initial[-1]["item"]["content"][0]["text"] = "canonical earlier text"
    responses.respond(*initial, *text_response("msg_final", "authoritative answer"))
    responses.respond(*text_response("msg_successor", "successor answer"))
    async with (
        timeout(15),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        turn = await session.submit("answer")
        frames = await read_body(stream_turn(turn, session=session))
        if earlier == "missing":
            with pytest.raises(
                NativeError,
                match="^Native model response ended without complete supported output$",
            ):
                await turn.get_result()
        else:
            assert (await turn.get_result()).final_output == "authoritative answer"
        assert len(responses.requests) == 1
        canonical = {
            item["id"]: "".join(part["text"] for part in item["content"])
            async for item in session.items()
            if item["type"] == "message" and item["role"] == "assistant"
        }
        if earlier == "missing":
            assert "msg_final" not in canonical and "broken" not in canonical
        else:
            assert canonical["msg_final"] == "authoritative answer"
        assert (
            canonical.get("msg_superseded")
            == {
                "missing": None,
                "different": "canonical earlier text",
                "valid": "earlier displayed text",
            }[earlier]
        )
        assert [
            f["delta"] for f in frames if isinstance(f, dict) and f["type"] == "text-delta"
        ] == (
            ["earlier displayed text"]
            if earlier == "missing"
            else ["earlier displayed text", "authoritative answer"]
        )
        terminal = {
            "valid": {"type": "finish"},
            "missing": {"type": "error", "errorText": "turn_failed"},
            "different": {"type": "error", "errorText": "resync_required"},
        }[earlier]
        assert frames[-2:] == [terminal, "[DONE]"]
        assert not any(frame == {"type": "abort"} for frame in frames)
        later = await session.submit("next")
        next_frames = await read_body(stream_turn(later, session=session))
        assert later.turn_id != turn.turn_id
        assert (await later.get_result()).final_output == "successor answer"
        assert next_frames[-2:] == [{"type": "finish"}, "[DONE]"]
        assert len(responses.requests) == 2
        replay = json.dumps(responses.requests[1]["input"])
        assert "broken" not in replay
        if earlier == "missing":
            assert "earlier displayed text" not in replay
            assert "authoritative answer" not in replay


@pytest.mark.parametrize(
    ("text", "final_output"),
    [(" \n\t ", ""), ("\u00a0\u2003", ""), ("\x1c", "\x1c")],
    ids=["ascii-blank", "unicode-blank", "rust-nonblank-separator"],
)
async def test_observed_final_text_accepts_native_blank_normalization(
    responses: ResponsesFixture, text: str, final_output: str
) -> None:
    from tests.test_native_response_integrity import full_envelopes

    full_envelopes(responses)
    events = text_response("msg_blank_final", text)
    events[-1]["item"].update(status="completed", phase="final_answer")
    responses.respond(*events)
    async with (
        timeout(15),
        Agent(
            model="gpt-5.2", instructions="Answer.", provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("Give the synthetic answer.")
        frames = await read_body(stream_turn(turn, session=session))
        assert (await turn.get_result()).final_output == final_output
        recorded = [item async for item in session.items() if item["type"] == "message"]
        assert recorded[-1]["id"] == "msg_blank_final"
        assert "".join(part["text"] for part in recorded[-1]["content"]) == text
        assert frames[-2:] == [{"type": "finish"}, "[DONE]"]
        assert {"type": "text-delta", "id": "msg_blank_final", "delta": text} in frames
        assert len(responses.requests) == 1


@pytest.mark.parametrize("mismatch", ["nonblank-text", "blank-item-id"])
async def test_final_comparison_keeps_exact_text_and_native_item_identity(
    responses: ResponsesFixture, mismatch: str
) -> None:
    from tests.test_native_response_integrity import full_envelopes

    full_envelopes(responses)
    shown = " answer " if mismatch == "nonblank-text" else " \n\t "
    events = text_response("msg_observed", shown)
    events[-1]["item"].update(status="completed", phase="final_answer")
    if mismatch == "nonblank-text":
        events[-1]["item"]["content"][0]["text"] = "answer"
    else:
        events[-1]["item"]["id"] = "msg_other_final"
    responses.respond(*events)
    async with (
        timeout(15),
        Agent(
            model="gpt-5.2", instructions="Answer.", provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("Give the synthetic answer.")
        frames = await read_body(stream_turn(turn, session=session))
        assert (await turn.get_result()).final_output == (
            "answer" if mismatch == "nonblank-text" else ""
        )
        assert {"type": "text-delta", "id": "msg_observed", "delta": shown} in frames
        assert frames[-2:] == [{"type": "error", "errorText": "resync_required"}, "[DONE]"]
        assert len(responses.requests) == 1


@pytest.mark.parametrize("output", ["Final answer", ""])
@pytest.mark.parametrize(
    "observed", [False, True], ids=["missing-commentary", "observed-commentary"]
)
async def test_completion_requires_all_canonical_commentary(responses, output, observed):
    commentary = assistant_output("Retained commentary")
    commentary["item"].update(id="ui_commentary", phase="commentary")
    final = assistant_output(output)
    final["item"].update(id="ui_final", phase="final_answer")
    events = text_response("ui_commentary", "Retained commentary") if observed else [commentary]
    if observed:
        events[-1]["item"]["phase"] = "commentary"
    if output:
        final_events = text_response("ui_final", output)
        final_events[-1]["item"]["phase"] = "final_answer"
        events.extend(final_events)
    else:
        events.append(final)
    responses.respond(*events)
    async with Agent(instructions="Reply.", provider=provider(responses)).session() as session:
        turn = await session.submit("Reply.")
        frames = await read_body(stream_turn(turn, session=session))
        assert (await turn.get_result()).final_output == output
        finish = observed and bool(output)
        assert ({"type": "finish"} in frames) is finish
        assert frames[-2:] == (
            [{"type": "finish"}, "[DONE]"]
            if finish
            else [{"type": "error", "errorText": "resync_required"}, "[DONE]"]
        )
        assert len(responses.requests) == 1
