"""Completed Responses content must not disappear into a successful empty turn."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agents import Agent, NativeError, function_tool
from agents.observation import TurnFinished
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider

REFUSAL = "I cannot provide the synthetic requested answer."
ERROR = "Native model response ended without complete supported output"
PROVIDER_OUTPUTS = [
    pytest.param(
        {"type": "function_call_output", "call_id": "provider-only", "output": "untrusted"},
        id="function-output",
    ),
    pytest.param(
        {"type": "custom_tool_call_output", "call_id": "provider-only", "output": "untrusted"},
        id="custom-output",
    ),
    pytest.param(
        {
            "type": "tool_search_output",
            "call_id": "provider-only",
            "status": "completed",
            "execution": "client",
            "tools": [],
        },
        id="search-output",
    ),
]


def refusal(*, output_index: int = 0, added_with_refusal: bool = False) -> list[dict[str, Any]]:
    content = {"type": "refusal", "refusal": REFUSAL}
    message = {
        "type": "message",
        "id": "msg_fixture_refusal",
        "role": "assistant",
        "status": "completed",
        "content": [content],
    }
    added = {
        **message,
        "status": "in_progress",
        "content": [{"type": "refusal", "refusal": ""}] if added_with_refusal else [],
    }
    indices = {
        "item_id": message["id"],
        "output_index": output_index,
        "content_index": 0,
    }
    return [
        {"type": "response.output_item.added", "output_index": output_index, "item": added},
        {
            "type": "response.content_part.added",
            **indices,
            "part": {"type": "refusal", "refusal": ""},
        },
        {"type": "response.refusal.delta", **indices, "delta": "I cannot provide "},
        {"type": "response.refusal.delta", **indices, "delta": "the synthetic requested answer."},
        {"type": "response.refusal.done", **indices, "refusal": REFUSAL},
        {"type": "response.content_part.done", **indices, "part": content},
        {"type": "response.output_item.done", "output_index": output_index, "item": message},
    ]


def full_envelopes(peer: ResponsesFixture, *, continue_first: bool = False) -> None:
    """Give each response complete terminal metadata and its actual streamed output."""
    sequence = 0
    completed_items: list[dict[str, Any]] = []

    async def fill(event: dict[str, Any]) -> None:
        nonlocal sequence
        if event["type"] == "response.created":
            sequence = 0
            completed_items.clear()
        event["sequence_number"] = sequence
        sequence += 1
        if event["type"] == "response.output_item.done":
            completed_items.append(event["item"])
        if event["type"] in {"response.created", "response.completed"}:
            created = event["type"] == "response.created"
            event["response"].update(
                created_at=1792200346,
                model="gpt-5.2",
                object="response",
                parallel_tool_calls=False,
                tool_choice="auto",
                tools=[],
                error=None,
                incomplete_details=None,
                status="in_progress" if created else "completed",
                output=[] if created else completed_items.copy(),
            )
            if not created and continue_first and len(peer.requests) == 1:
                event["response"]["end_turn"] = False

    peer.before_event = fill


def phased_answer(
    text: str, *, final_phase: str | None = "final_answer", trailing_untagged_blank: bool = False
) -> list[dict[str, Any]]:
    """Two genuinely completed messages in one model generation."""
    events: list[dict[str, Any]] = []
    messages = [("commentary", "Synthetic intermediate commentary"), (final_phase, text)]
    if trailing_untagged_blank:
        messages.append((None, " \n\t "))
    for index, (phase, content) in enumerate(messages):
        done = assistant_output(content)
        done["item"]["status"] = "completed"
        if phase is not None:
            done["item"]["phase"] = phase
        events.extend(
            (
                {
                    "type": "response.output_item.added",
                    "output_index": index,
                    "item": {**done["item"], "status": "in_progress", "content": []},
                },
                {**done, "output_index": index},
            )
        )
    return events


def assert_phased_answer_in_history(
    history: list[dict[str, Any]], events: list[dict[str, Any]]
) -> None:
    completed = [event["item"] for event in events if event["type"] == "response.output_item.done"]
    identifiers = {item["id"] for item in completed}
    recorded = [
        row["payload"]
        for row in history
        if row["type"] == "response_item" and row["payload"].get("id") in identifiers
    ]
    assert [
        (item["id"], item["role"], item.get("phase"), item["content"]) for item in recorded
    ] == [(item["id"], item["role"], item.get("phase"), item["content"]) for item in completed]


@pytest.mark.parametrize(
    ("text", "trailing_untagged_blank", "expected"),
    [
        ("", False, ""),
        (" \n\t ", False, ""),
        ("supported final", False, "supported final"),
        ("\x1c", False, "\x1c"),
        ("", True, ""),
    ],
    ids=[
        "blank",
        "whitespace",
        "nonblank",
        "rust-nonblank-separator",
        "untagged-after-final-blank",
    ],
)
async def test_final_typed_text_replaces_commentary_and_belongs_to_exact_root_turn(
    responses: ResponsesFixture, text: str, trailing_untagged_blank: bool, expected: str
) -> None:
    full_envelopes(responses)
    answer = phased_answer(text, trailing_untagged_blank=trailing_untagged_blank)
    responses.respond(*answer)
    responses.respond(assistant_output("fresh root answer"))
    async with (
        timeout(30),
        Agent(
            model="gpt-5.2", instructions="Answer.", provider=provider(responses)
        ).session() as session,
    ):
        first = await session.submit("complete the answer")
        result = await first.get_result()
        assert result.turn is first
        observations = [event async for event in first.events()]
        assert isinstance(observations[-1], TurnFinished)
        assert (observations[-1].turn_id, observations[-1].status) == (first.turn_id, "completed")
        saved = await session.save_state()
        assert_phased_answer_in_history(saved.history, answer)
        later = await session.submit("answer a fresh question")
        assert later.turn_id != first.turn_id
        assert (await later.get_result()).final_output == "fresh root answer"
        captured = await first.get_result()
        assert captured.turn is first and captured.usage == result.usage
        assert captured.final_output == result.final_output
        assert len(responses.requests) == 2
        assert result.final_output == expected


@pytest.mark.parametrize("earlier_turn", [False, True])
async def test_full_refusal_fails_exact_turn_and_does_not_expose_payload(
    responses: ResponsesFixture, earlier_turn: bool
) -> None:
    full_envelopes(responses)
    if earlier_turn:
        responses.respond(assistant_output("earlier accepted answer"))
    responses.respond(*refusal())
    responses.respond(assistant_output("fresh next answer"))
    async with (
        timeout(30),
        Agent(
            model="gpt-5.2", instructions="Answer.", provider=provider(responses)
        ).session() as session,
    ):
        if earlier_turn:
            previous = await session.submit("first")
            assert (await previous.get_result()).final_output == "earlier accepted answer"
        turn = await session.submit("please decline the synthetic test")
        with pytest.raises(NativeError, match=ERROR) as caught:
            await turn.get_result()
        assert REFUSAL not in str(caught.value)
        assert "msg_fixture_refusal" not in repr(caught.value)
        events = [event async for event in turn.events()]
        assert isinstance(events[-1], TurnFinished)
        assert events[-1].status == "failed"
        assert events[-1].error_type == "NativeError"
        assert REFUSAL not in repr(events)
        following = await session.submit("continue")
        assert following.turn_id != turn.turn_id
        assert (await following.get_result()).final_output == "fresh next answer"
    assert len(responses.requests) == (3 if earlier_turn else 2)


async def test_refusal_after_commentary_in_same_response_cannot_return_the_commentary(
    responses: ResponsesFixture,
) -> None:
    full_envelopes(responses)
    comment = assistant_output("synthetic progress, not an answer")
    comment["item"]["phase"] = "commentary"
    responses.respond(comment, *refusal(output_index=1))
    async with (
        timeout(30),
        Agent(
            model="gpt-5.2", instructions="Answer.", provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("finish the answer")
        with pytest.raises(NativeError, match=ERROR):
            await turn.get_result()
        assert len(responses.requests) == 1


@pytest.mark.parametrize("earlier", ["", "synthetic intermediate commentary"])
async def test_visible_final_message_without_text_does_not_inherit_an_earlier_item(
    responses: ResponsesFixture, earlier: str
) -> None:
    full_envelopes(responses)
    commentary = assistant_output(earlier)
    commentary["item"].update(status="completed", phase="commentary")
    final = assistant_output("")
    final["item"].update(status="completed", phase="final_answer", content=[])
    responses.respond(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**commentary["item"], "status": "in_progress", "content": []},
        },
        {**commentary, "output_index": 0},
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {**final["item"], "status": "in_progress"},
        },
        {**final, "output_index": 1},
    )
    responses.respond(assistant_output("fresh next turn"))
    async with (
        timeout(30),
        Agent(
            model="gpt-5.2", instructions="Answer.", provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("finish after the interim message")
        with pytest.raises(NativeError, match=ERROR) as caught:
            await turn.get_result()
        assert final["item"]["id"] not in str(caught.value)
        assert "synthetic intermediate commentary" not in str(caught.value)
        events = [event async for event in turn.events()]
        assert isinstance(events[-1], TurnFinished)
        assert events[-1].status == "failed"
        following = await session.submit("continue")
        assert following.turn_id != turn.turn_id
        assert (await following.get_result()).final_output == "fresh next turn"
    assert len(responses.requests) == 2


async def test_native_tool_progress_survives_a_later_empty_assistant_placeholder(
    responses: ResponsesFixture,
) -> None:
    full_envelopes(responses)
    effects: list[str] = []

    @function_tool
    async def read_record() -> str:
        """Read the selected record."""
        effects.append("read")
        return "fixture record"

    placeholder = assistant_output("")
    placeholder["item"].update(status="completed", content=[])
    responses.respond(
        function_call("read_record", {}, "read-record"),
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {**placeholder["item"], "status": "in_progress"},
        },
        {**placeholder, "output_index": 1},
    )
    responses.respond(assistant_output("answer after tool progress"))
    async with (
        timeout(30),
        Agent(
            model="gpt-5.2",
            instructions="Answer.",
            tools=[read_record],
            provider=provider(responses),
        ).session() as session,
    ):
        turn = await session.submit("read then answer")
        result = await turn.get_result()
        assert result.turn is turn
        assert result.final_output == "answer after tool progress"
        assert effects == ["read"]
        assert len(responses.requests) == 2
        assert tool_output(responses.requests[1], "read-record") == "fixture record"


async def test_native_tool_progress_does_not_hide_a_later_unfinished_assistant_item(
    responses: ResponsesFixture,
) -> None:
    full_envelopes(responses)

    @function_tool
    async def read_record() -> str:
        """Read the selected record."""
        return "fixture record"

    responses.respond(function_call("read_record", {}, "read-record"), *refusal(output_index=1))
    async with (
        timeout(30),
        Agent(
            model="gpt-5.2",
            instructions="Answer.",
            tools=[read_record],
            provider=provider(responses),
        ).session() as session,
    ):
        turn = await session.submit("read then answer")
        with pytest.raises(NativeError, match=ERROR):
            await turn.get_result()
        assert len(responses.requests) == 1
        events = [event async for event in turn.events()]
        assert isinstance(events[-1], TurnFinished)
        assert events[-1].status == "failed"


@pytest.mark.parametrize(
    "item",
    [
        *PROVIDER_OUTPUTS,
        pytest.param(
            {"type": "additional_tools", "role": "assistant", "tools": []},
            id="additional-tools",
        ),
        pytest.param(
            {
                "type": "tool_search_call",
                "call_id": "provider-only",
                "status": "completed",
                "execution": "server",
                "arguments": {},
            },
            id="server-search",
        ),
        pytest.param(
            {
                "type": "tool_search_call",
                "call_id": None,
                "status": "completed",
                "execution": "client",
                "arguments": {},
            },
            id="search-without-call-id",
        ),
    ],
)
@pytest.mark.parametrize("after_unfinished", [False, True], ids=["alone", "after-refusal"])
async def test_decoded_nonprogress_item_cannot_satisfy_or_clear_required_assistant_output(
    responses: ResponsesFixture, item: dict[str, Any], after_unfinished: bool
) -> None:
    full_envelopes(responses)
    prefix: list[dict[str, Any]] = []
    if after_unfinished:
        commentary = assistant_output("synthetic earlier commentary")
        commentary["item"].update(status="completed", phase="commentary")
        # Core sees this assistant opening; only its refusal Done is undecodable.
        prefix = [commentary, *refusal(output_index=1)]
    responses.respond(
        *prefix,
        {
            "type": "response.output_item.done",
            "output_index": 2 if after_unfinished else 0,
            "item": item,
        },
    )
    responses.respond(assistant_output("fresh following answer"))
    async with (
        timeout(30),
        Agent(
            model="gpt-5.2", instructions="Answer.", provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("answer using supported output")
        with pytest.raises(NativeError, match=ERROR) as caught:
            await turn.get_result()
        assert str(caught.value) == ERROR
        events = [event async for event in turn.events()]
        assert isinstance(events[-1], TurnFinished)
        assert events[-1].status == "failed"
        assert len(responses.requests) == 1
        following = await session.submit("answer the next request")
        assert following.turn_id != turn.turn_id
        assert (await following.get_result()).final_output == "fresh following answer"
    assert len(responses.requests) == 2


@pytest.mark.parametrize("item", PROVIDER_OUTPUTS)
@pytest.mark.parametrize("event_kind", ["added", "done"])
async def test_provider_receipt_after_canonical_text_fails_before_history_is_recorded(
    responses: ResponsesFixture, item: dict[str, Any], event_kind: str
) -> None:
    full_envelopes(responses)
    responses.respond(
        assistant_output("canonical before receipt"),
        {
            "type": f"response.output_item.{event_kind}",
            "output_index": 1,
            "item": item,
        },
    )
    responses.respond(assistant_output("fresh following answer"))
    async with (
        timeout(30),
        Agent(
            model="gpt-5.2", instructions="Answer.", provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("answer with supported output")
        with pytest.raises(NativeError, match=ERROR) as caught:
            await turn.get_result()
        assert str(caught.value) == ERROR
        assert caught.value.response_integrity_reason == "provider_owned_tool_receipt"
        events = [event async for event in turn.events()]
        assert isinstance(events[-1], TurnFinished)
        assert events[-1].status == "failed"
        assert len(responses.requests) == 1
        following = await session.submit("answer the next request")
        assert following.turn_id != turn.turn_id
        assert (await following.get_result()).final_output == "fresh following answer"
    assert len(responses.requests) == 2
    assert "provider-only" not in json.dumps(responses.requests[1]["input"])


@pytest.mark.parametrize(
    "item",
    [
        pytest.param(
            {
                "type": "function_call_output",
                "name": "notifications",
                "namespace": "fixture",
                "output": "External fixture notification.",
            },
            id="external-named-output",
        ),
        pytest.param(
            {
                "type": "tool_search_output",
                "call_id": "server-search-context",
                "status": "completed",
                "execution": "server",
                "tools": [],
            },
            id="server-search-output",
        ),
    ],
)
async def test_supported_external_context_survives_with_real_answer_and_following_turn(
    responses: ResponsesFixture, item: dict[str, Any]
) -> None:
    full_envelopes(responses)
    responses.respond(
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        assistant_output("real answer after external context"),
    )
    responses.respond(assistant_output("next distinct answer"))
    async with (
        timeout(30),
        Agent(
            model="gpt-5.2", instructions="Answer.", provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("answer from supplied context")
        result = await turn.get_result()
        assert result.turn is turn
        assert result.final_output == "real answer after external context"
        assert len(responses.requests) == 1
        following = await session.submit("answer the next request")
        assert following.turn_id != turn.turn_id
        assert (await following.get_result()).final_output == "next distinct answer"
    assert len(responses.requests) == 2
    assert any(
        all(row.get(key) == value for key, value in item.items())
        for row in responses.requests[1]["input"]
    )


async def test_same_turn_continuation_cannot_mask_a_later_refusal(
    responses: ResponsesFixture,
) -> None:
    full_envelopes(responses, continue_first=True)
    responses.respond(assistant_output("accepted interim response"))
    responses.respond(*refusal())
    async with (
        timeout(30),
        Agent(
            model="gpt-5.2", instructions="Answer.", provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("finish the answer")
        with pytest.raises(NativeError, match=ERROR):
            await turn.get_result()
        assert len(responses.requests) == 2
        assert "accepted interim response" in json.dumps(responses.requests[1]["input"])


async def test_tool_only_generation_still_runs_before_later_refusal(
    responses: ResponsesFixture,
) -> None:
    full_envelopes(responses)
    effects: list[str] = []

    @function_tool
    async def read_record() -> str:
        """Read the selected record."""
        effects.append("read")
        return "fixture record"

    responses.respond(function_call("read_record", {}, "read-record"))
    responses.respond(*refusal())
    async with (
        timeout(30),
        Agent(
            model="gpt-5.2",
            instructions="Answer.",
            tools=[read_record],
            provider=provider(responses),
        ).session() as session,
    ):
        turn = await session.submit("read then answer")
        with pytest.raises(NativeError, match=ERROR):
            await turn.get_result()
        assert effects == ["read"]
        assert len(responses.requests) == 2
        assert tool_output(responses.requests[1], "read-record") == "fixture record"


@pytest.mark.parametrize("text", ["", " \n\t "])
async def test_valid_blank_output_text_succeeds_and_remains_in_history(
    responses: ResponsesFixture, text: str
) -> None:
    full_envelopes(responses)
    blank = assistant_output(text)
    blank["item"]["status"] = "completed"
    responses.respond(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**blank["item"], "status": "in_progress", "content": []},
        },
        blank,
    )
    responses.respond(assistant_output("after blank"))
    async with (
        timeout(30),
        Agent(
            model="gpt-5.2", instructions="Answer.", provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("blank answer")
        result = await turn.get_result()
        assert result.turn is turn
        # Preserve Core's existing presentation of blank text, not a refusal error.
        assert result.final_output == ""
        events = [event async for event in turn.events()]
        assert isinstance(events[-1], TurnFinished)
        assert events[-1].status == "completed"
        following = await session.submit("continue")
        assert (await following.get_result()).final_output == "after blank"
        accepted = [
            item for item in responses.requests[1]["input"] if item.get("id") == blank["item"]["id"]
        ]
        assert len(accepted) == 1
        assert accepted[0]["content"] == [{"type": "output_text", "text": text}]


@pytest.mark.parametrize("kind", ["reasoning", "empty_message"])
async def test_terminal_response_without_an_assistant_text_part_fails_and_recovers(
    responses: ResponsesFixture, kind: str
) -> None:
    full_envelopes(responses)
    if kind == "reasoning":
        item: dict[str, Any] = {
            "type": "reasoning",
            "id": "rs_fixture_private",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": "synthetic non-answer summary"}],
        }
        added = {**item, "status": "in_progress", "summary": []}
    else:
        item = {
            "type": "message",
            "id": "msg_fixture_empty",
            "role": "assistant",
            "status": "completed",
            "content": [],
        }
        added = {**item, "status": "in_progress"}
    responses.respond(
        {"type": "response.output_item.added", "output_index": 0, "item": added},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
    )
    responses.respond(assistant_output("fresh answer after unsupported content"))
    async with (
        timeout(30),
        Agent(
            model="gpt-5.2", instructions="Answer.", provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("answer this request")
        with pytest.raises(NativeError, match=ERROR) as caught:
            await turn.get_result()
        assert item["id"] not in str(caught.value)
        assert caught.value.response_integrity_reason == "missing_supported_terminal_output"
        assert "synthetic non-answer summary" not in repr(caught.value)
        events = [event async for event in turn.events()]
        assert isinstance(events[-1], TurnFinished)
        assert events[-1].status == "failed"
        assert events[-1].error_type == "NativeError"
        # An explicitly failed model turn must not strand this public Session.
        following = await session.submit("continue")
        assert following.turn_id != turn.turn_id
        assert (
            await following.get_result()
        ).final_output == "fresh answer after unsupported content"
    assert len(responses.requests) == 2


@pytest.mark.parametrize("text", ["", " \n\t ", "native supported input_text answer"])
async def test_core_supported_assistant_input_text_preserves_result(
    responses: ResponsesFixture, text: str
) -> None:
    full_envelopes(responses)
    message = assistant_output(text)
    message["item"]["content"] = [{"type": "input_text", "text": text}]
    message["item"]["status"] = "completed"
    responses.respond(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**message["item"], "status": "in_progress", "content": []},
        },
        message,
    )
    async with (
        timeout(30),
        Agent(
            model="gpt-5.2", instructions="Answer.", provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("answer")
        result = await turn.get_result()
        assert result.final_output == (text if text.strip() else "")
        assert result.turn is turn
    assert len(responses.requests) == 1


async def test_nonterminal_reasoning_only_continues_until_the_same_turn_has_an_answer(
    responses: ResponsesFixture,
) -> None:
    full_envelopes(responses, continue_first=True)
    responses.respond(
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "reasoning",
                "id": "rs_fixture_continuing",
                "status": "completed",
                "summary": [{"type": "summary_text", "text": "synthetic interim reasoning"}],
            },
        }
    )
    responses.respond(assistant_output("completed after interim reasoning"))
    async with (
        timeout(30),
        Agent(
            model="gpt-5.2", instructions="Answer.", provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("finish reasoning")
        result = await turn.get_result()
        assert result.turn is turn
        assert result.final_output == "completed after interim reasoning"
        events = [event async for event in turn.events()]
        assert isinstance(events[-1], TurnFinished)
        assert events[-1].status == "completed"
    assert len(responses.requests) == 2


async def test_refusal_with_both_added_and_done_undecodable_after_commentary(
    responses: ResponsesFixture,
) -> None:
    full_envelopes(responses)
    comment = assistant_output("synthetic interim")
    comment["item"]["phase"] = "commentary"
    responses.respond(comment, *refusal(output_index=1, added_with_refusal=True))
    async with (
        timeout(30),
        Agent(
            model="gpt-5.2", instructions="Answer.", provider=provider(responses)
        ).session() as session,
    ):
        reported = False
        try:
            await (await session.submit("finish the answer")).get_result()
        except NativeError as error:
            reported = ERROR in str(error)
        assert reported, "An undecodable response must report an explicit error"


@pytest.mark.parametrize("websocket", [False, True], ids=["http", "websocket"])
@pytest.mark.parametrize("invalid_phase", ["added", "done"])
async def test_undecodable_item_fails_exact_turn_without_retry_or_effect_replay(
    websocket: bool, invalid_phase: str
) -> None:
    from tests.test_native_provider_websockets import peer

    effects: list[str] = []

    @function_tool
    async def record_effect() -> str:
        """Record the synthetic effect once."""
        effects.append("recorded")
        return "effect completed"

    async with timeout(30), peer() as server:
        full_envelopes(server)
        server.respond(function_call("record_effect", {}, "completed-effect"))
        commentary = assistant_output("synthetic interim")
        commentary["item"]["phase"] = "commentary"
        invalid = refusal(output_index=1, added_with_refusal=invalid_phase == "added")
        server.expected_disconnect_event = invalid[0] if invalid_phase == "added" else invalid[-1]
        server.respond(
            commentary,
            *invalid,
            function_call("record_effect", {}, "must-not-run-after-invalid-item"),
        )
        server.respond(assistant_output("fresh successor"))
        async with Agent(
            model="gpt-5.2",
            instructions="Record the effect then answer.",
            tools=[record_effect],
            provider=server.provider(websocket=websocket),
        ).session() as session:
            failed = await session.submit("record then answer")
            with pytest.raises(NativeError, match=ERROR) as caught:
                await failed.get_result()
            assert str(caught.value) == ERROR
            assert caught.value.response_integrity_reason == "undecodable_output_item"
            assert effects == ["recorded"]
            assert len(server.requests) == 2
            events = [event async for event in failed.events()]
            assert isinstance(events[-1], TurnFinished)
            assert (events[-1].turn_id, events[-1].status) == (failed.turn_id, "failed")
            assert events[-1].error_type == "NativeError"
            history = [entry async for entry in session.items()]
            for private in (REFUSAL, "msg_fixture_refusal", "must-not-run-after-invalid-item"):
                assert private not in repr(caught.value)
                assert private not in repr(events)
                assert private not in repr(history)
            successor = await session.submit("fresh question")
            assert successor.turn_id != failed.turn_id
            result = await successor.get_result()
            assert result.turn is successor and result.final_output == "fresh successor"
            with pytest.raises(NativeError, match=ERROR):
                await failed.get_result()
            assert effects == ["recorded"]
            assert len(server.requests) == 3
            assert REFUSAL not in json.dumps(server.requests[-1]["input"])
            if websocket:
                assert server.connections == 2 and not server.http_requests


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("consume_events", [False, True])
async def test_undecodable_native_child_output_keeps_sibling_root_and_successor_usable(
    tmp_path, monkeypatch, consume_events
) -> None:
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import agent_for, child_peer, handles, trust
    from tests.test_native_executor_tools import declaration

    async with (
        timeout(30),
        child_peer(("default", "research")) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        server.command = "printf proof >> integrity-effects"
        injected = False
        original = server.response

        def response():
            nonlocal injected
            events = original()
            if not injected and events[0].get("item", {}).get("id") == "done-alpha":
                injected = True
                events[0]["item"]["phase"] = "commentary"
                return [events[0], *refusal(output_index=1, added_with_refusal=True)]
            return events

        server.response = response
        for release in server.release.values():
            release.set()
        async with agent_for(server, executor).session(
            environment=declaration(executor)
        ) as session:
            root = await session.submit("start alpha and beta")
            assert (await root.get_result()).final_output == "root complete"
            turns = {turn.child_id: turn for turn in await handles(session, 2)}
            for entered in server.entered.values():
                await entered.wait()
            alpha_id = server.child_metadata["alpha"][0]["thread_id"]
            beta_id = server.child_metadata["beta"][0]["thread_id"]
            alpha, beta = turns[alpha_id], turns[beta_id]

            async def collect_child():
                return [event async for event in alpha.events()]

            consumed = asyncio.create_task(collect_child()) if consume_events else None
            with pytest.raises(NativeError) as caught:
                await alpha.get_result()
            # Native child handles retain a sanitized terminal receipt.
            assert str(caught.value) in {ERROR, "Native child turn failed"}
            assert caught.value.response_integrity_reason in {None, "undecodable_output_item"}
            beta_result = await beta.get_result()
            assert beta_result.turn is beta and beta_result.final_output == "beta complete"
            events = await consumed if consumed is not None else await collect_child()
            assert isinstance(events[-1], TurnFinished)
            assert (events[-1].turn_id, events[-1].status) == (alpha.turn_id, "failed")
            assert len(server.child_requests["alpha"]) == 2
            assert len(server.child_requests["beta"]) == 2
            effects = executor.root / "integrity-effects"
            assert effects.read_text() == "proofproof"
            history = [entry async for entry in session.items(subagent_id=alpha_id)]
            for private in (REFUSAL, "msg_fixture_refusal"):
                assert private not in repr(caught.value)
                assert private not in repr(events)
                assert private not in repr(history)
            children = {child.id: child async for child in session.subagents()}
            successor = await children[alpha_id].start("fresh child question")
            assert successor.turn_id != alpha.turn_id
            assert (await successor.get_result()).final_output == "alpha complete"
            assert len(server.child_requests["alpha"]) == 3
            assert effects.read_text() == "proofproof"
            assert (await beta.get_result()).final_output == "beta complete"
            assert (await (await session.submit("continue root")).get_result()).final_output == (
                "root complete"
            )


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("consume_events", [False, True])
async def test_owned_native_child_parser_reason_after_public_admission(
    tmp_path, monkeypatch, consume_events
) -> None:
    from agents.observation import TurnStarted
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import agent_for, child_peer, trust
    from tests.test_native_executor_tools import declaration

    async with (
        timeout(30),
        child_peer(("default",)) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        original = server.response

        def response():
            events = original()
            if events[0].get("item", {}).get("id") == "done-alpha":
                events[0]["item"]["phase"] = "commentary"
                return [events[0], *refusal(output_index=1, added_with_refusal=True)]
            return events

        server.response = response
        async with agent_for(server, executor).session(
            environment=declaration(executor)
        ) as session:
            root = await session.submit("start alpha and beta")
            assert (await root.get_result()).final_output == "root complete"
            await server.entered["alpha"].wait()
            alpha_id = server.child_metadata["alpha"][0]["thread_id"]
            # Provider entry alone does not prove the Session reader has captured
            # the child. Match a public started observation to its live handle
            # while the final response remains held behind the existing gate.
            alpha = None
            async for event in session.events():
                if not isinstance(event, TurnStarted):
                    continue
                children = {child.id: child async for child in session.subagents()}
                candidate = children[alpha_id].turn
                if candidate is not None and event.submission_id == candidate.submission_id:
                    assert event.turn_id == candidate.turn_id
                    alpha = candidate
                    break
            assert alpha is not None
            assert not server.release["alpha"].is_set()
            waiting = asyncio.Event()
            observing = asyncio.Event()

            async def wait_child():
                waiting.set()
                return await alpha.get_result()

            async def collect_child():
                observing.set()
                return [event async for event in alpha.events()]

            result = asyncio.create_task(wait_child())
            await waiting.wait()
            consumed = asyncio.create_task(collect_child()) if consume_events else None
            if consumed is not None:
                await observing.wait()
            assert not result.done()
            server.release["alpha"].set()
            with pytest.raises(NativeError) as caught:
                await result
            assert str(caught.value) in {ERROR, "Native child turn failed"}
            assert caught.value.response_integrity_reason == "undecodable_output_item"
            events = await consumed if consumed is not None else await collect_child()
            assert isinstance(events[-1], TurnFinished)
            assert (events[-1].turn_id, events[-1].status) == (alpha.turn_id, "failed")
            assert len(server.child_requests["alpha"]) == 2
            assert (executor.root / "alpha.txt").read_text() == "alpha"
            assert REFUSAL not in repr(caught.value)
            assert REFUSAL not in repr(events)


def terminal_reason_for_test(reason: str) -> str:
    return (
        "missing_supported_terminal_output"
        if reason != "missing_supported_terminal_output"
        else "unfinished_assistant_item"
    )


@pytest.mark.parametrize("notice_first", [False, True], ids=["terminal-first", "notice-first"])
@pytest.mark.parametrize("consume_events", [False, True])
@pytest.mark.parametrize(
    "reason",
    [
        "undecodable_output_item",
        "provider_owned_tool_receipt",
        "unfinished_assistant_item",
        "missing_supported_terminal_output",
    ],
)
async def test_public_integrity_reason_follows_selected_notice_without_terminal_enrichment(
    responses: ResponsesFixture,
    monkeypatch,
    reason: str,
    notice_first: bool,
    consume_events: bool,
) -> None:
    """Control trusted notice delivery while real native work supplies the terminal.

    Native queue ownership is covered by Rust receipt tests. This boundary test
    fixes delivery order rather than assuming a tokio select scheduling order.
    Parser errors do not emit notices; their normal path is passive terminal conversion.
    """
    from agents import _native

    original_next = _native.NativeThread._next_session_event
    pending: list[str] = []
    release_late = asyncio.Event()
    selected = False

    async def ordered_next(native):
        nonlocal selected
        if pending:
            if not notice_first:
                await release_late.wait()
            return pending.pop(0)
        while True:
            raw = await original_next(native)
            event = json.loads(raw)
            if event is None:
                return raw
            message = event.get("msg", {})
            if message.get("type") == "_sdk_model_generation_failed":
                continue
            if (
                not selected
                and message.get("type") in {"task_complete", "turn_complete"}
                and message.get("error")
            ):
                selected = True
                terminal_reason = (
                    "missing_supported_terminal_output"
                    if reason != "missing_supported_terminal_output"
                    else "unfinished_assistant_item"
                )
                message["error"]["_sdk_response_integrity_reason"] = terminal_reason
                raw = json.dumps(event)
                notice = json.dumps(
                    {
                        "id": event["id"],
                        "msg": {
                            "type": "_sdk_model_generation_failed",
                            "turn_id": event["id"],
                            "message": ERROR,
                            "response_integrity_reason": reason,
                            "policy_stop": False,
                        },
                    }
                )
                pending.append(raw if notice_first else notice)
                return notice if notice_first else raw
            return raw

    monkeypatch.setattr(_native.NativeThread, "_next_session_event", ordered_next)
    full_envelopes(responses)
    if reason == "undecodable_output_item":
        responses.respond(*refusal(added_with_refusal=True))
    elif reason == "provider_owned_tool_receipt":
        responses.respond(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call_output",
                    "call_id": "provider-only",
                    "output": "synthetic",
                },
            }
        )
    elif reason == "unfinished_assistant_item":
        responses.respond(
            {
                "type": "response.output_item.added",
                "item": {"type": "message", "id": "unfinished", "role": "assistant", "content": []},
            }
        )
    else:
        responses.respond()
    responses.respond(assistant_output("fresh successor"))
    try:
        async with (
            timeout(30),
            Agent(
                model="gpt-5.2", instructions="Answer.", provider=provider(responses)
            ).session() as session,
        ):
            turn = await session.submit("fail then recover")

            async def collect():
                return [event async for event in turn.events()]

            stream = asyncio.create_task(collect()) if consume_events else None
            with pytest.raises(NativeError) as caught:
                await turn.get_result()
            error = caught.value
            assert error.response_integrity_reason == (
                reason if notice_first else terminal_reason_for_test(reason)
            )
            assert ERROR in str(error)
            assert error.effects == "unknown"
            release_late.set()
            with pytest.raises(NativeError) as repeated:
                await turn.get_result()
            assert repeated.value is error
            assert repeated.value.response_integrity_reason == (
                reason if notice_first else terminal_reason_for_test(reason)
            )
            if stream is not None:
                assert isinstance((await stream)[-1], TurnFinished)
            assert (
                await (await session.submit("successor")).get_result()
            ).final_output == "fresh successor"
            assert len(responses.requests) == 2
            assert error.response_integrity_reason == (
                reason if notice_first else terminal_reason_for_test(reason)
            )
    finally:
        release_late.set()


async def test_same_id_manual_recovery_does_not_decorate_the_original_public_failure(
    responses: ResponsesFixture,
) -> None:
    from agents import FunctionTool, InteractionRequired
    from agents.state import SessionState
    from tests.test_native_manual_tools import LookupArgs, next_request

    lookup = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    full_envelopes(responses)
    responses.respond(function_call("lookup", {"key": "A"}, "manual-A"))
    agent = Agent(
        model="gpt-5.5",
        instructions="Use lookup then answer.",
        provider=provider(responses),
        tools=[lookup],
    )
    async with timeout(30):
        async with agent.session() as original_session:
            original_turn = await original_session.submit("look up A")
            original_request = await next_request(original_session.interactions(), lookup)
            saved = await original_session.suspend()
            with pytest.raises(InteractionRequired) as original_error:
                await original_turn.get_result()
            assert original_error.value.response_integrity_reason is None
        async with agent.session(state=SessionState.from_json(saved.to_json())) as restored:
            current = await next_request(restored.interactions(), lookup)
            assert current.turn_id == original_request.turn_id == original_turn.turn_id
            with pytest.raises((ValueError, RuntimeError), match="foreign|expired|closed"):
                await original_request.respond("stale")
            responses.respond(*refusal(added_with_refusal=True))
            await current.respond("answer-A")
            assert restored.restored_turn is not None
            with pytest.raises(NativeError) as current_error:
                await restored.restored_turn.get_result()
            assert current_error.value.response_integrity_reason in {
                None,
                "undecodable_output_item",
            }
            assert original_error.value.response_integrity_reason is None
            assert len(responses.requests) == 2


async def test_parser_failure_settles_an_already_running_tool_without_new_early_cancellation(
    responses: ResponsesFixture,
) -> None:
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    effects: list[str] = []

    @function_tool
    async def settle_once() -> str:
        """Finish the already-started synthetic effect."""
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        effects.append("settled")
        return "settled"

    full_envelopes(responses)
    responses.respond(
        function_call("settle_once", {}, "owned-effect"),
        *refusal(output_index=1, added_with_refusal=True),
    )
    async with (
        timeout(30),
        Agent(
            model="gpt-5.2",
            instructions="Use settle_once then answer.",
            provider=provider(responses),
            tools=[settle_once],
        ).session() as session,
    ):
        turn = await session.submit("settle the started effect")
        outcome = asyncio.create_task(turn.get_result())
        await entered.wait()
        try:
            # Core must drain its in-flight tool before publishing the parser error.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(outcome), 0.1)
            assert not cancelled.is_set()
        finally:
            release.set()
        with pytest.raises(NativeError) as caught:
            await outcome
        assert str(caught.value) == ERROR
        assert caught.value.category == "internal_error"
        assert caught.value.response_integrity_reason == "undecodable_output_item"
        assert effects == ["settled"]
        assert not cancelled.is_set()
        assert len(responses.requests) == 1


@pytest.mark.parametrize("websocket", [False, True], ids=["http", "websocket"])
@pytest.mark.parametrize(
    "reason",
    [
        "undecodable_output_item",
        "provider_owned_tool_receipt",
        "unfinished_assistant_item",
        "missing_supported_terminal_output",
    ],
)
async def test_fresh_public_wire_failures_expose_their_originating_reason(
    websocket: bool,
    reason: str,
) -> None:
    from tests.test_native_provider_websockets import peer

    async with timeout(30), peer() as server:
        full_envelopes(server)
        if reason == "undecodable_output_item":
            server.respond(*refusal(added_with_refusal=True))
        elif reason == "provider_owned_tool_receipt":
            server.respond(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call_output",
                        "call_id": "provider-only",
                        "output": "synthetic",
                    },
                }
            )
        elif reason == "unfinished_assistant_item":
            server.respond(
                {
                    "type": "response.output_item.added",
                    "item": {
                        "type": "message",
                        "id": "pending",
                        "role": "assistant",
                        "content": [],
                    },
                }
            )
        else:
            server.respond()
        async with Agent(
            model="gpt-5.2", instructions="Answer.", provider=server.provider(websocket=websocket)
        ).session() as session:
            turn = await session.submit("answer with supported output")
            with pytest.raises(NativeError, match=ERROR) as caught:
                await turn.get_result()
            assert caught.value.response_integrity_reason == reason
            assert str(caught.value) == ERROR
            assert caught.value.effects == "unknown"
            assert len(server.requests) == 1
