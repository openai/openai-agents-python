"""Typed previews preserve native turn ownership and completed authority."""

from __future__ import annotations

import asyncio

import pytest

from agents import Agent, NativeProvider, function_tool
from agents.observation import (
    FunctionCallArgumentsDelta,
    RawProviderEvent,
    ReasoningSummaryTextDelta,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
)


@pytest.mark.parametrize("capture", [False, True])
async def test_reasoning_summary_preview_uses_existing_model_item_opt_in(responses, capture):
    release = asyncio.Event()
    summary = "synthetic visible summary"
    private = "synthetic private reasoning"
    reasoning = {"type": "reasoning", "id": "summary-item", "summary": []}
    responses.respond(
        {"type": "response.output_item.added", "output_index": 0, "item": reasoning},
        {
            "type": "response.reasoning_summary_part.added",
            "item_id": "summary-item",
            "output_index": 0,
            "summary_index": 0,
            "part": {"type": "summary_text", "text": ""},
        },
        {
            "type": "response.reasoning_summary_text.delta",
            "item_id": "summary-item",
            "output_index": 0,
            "summary_index": 0,
            "delta": summary,
        },
        {
            "type": "response.reasoning_text.delta",
            "item_id": "summary-item",
            "output_index": 0,
            "content_index": 0,
            "delta": private,
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {**reasoning, "summary": [{"type": "summary_text", "text": summary}]},
        },
        assistant_output("finished"),
    )

    async def before_event(event):
        if capture and event["type"] == "response.output_item.done":
            await release.wait()

    responses.before_event = before_event
    async with (
        timeout(15),
        NativeProvider(base_url=responses.url, api_key="synthetic-key") as provider,
    ):
        async with Agent(instructions="Reply.", model="gpt-5.1-codex", provider=provider).session(
            include_model_output_items=capture, observation_retention="all"
        ) as session:
            turn = await session.submit("Reason.")
            if capture:
                try:
                    async for event in turn.events():
                        if isinstance(event, ReasoningSummaryTextDelta):
                            assert (event.item_id, event.summary_index, event.delta) == (
                                "summary-item",
                                0,
                                summary,
                            )
                            assert event.turn_id == turn.turn_id
                            assert event.submission_id == turn.submission_id
                            break
                    else:
                        pytest.fail("missing summary preview before completed output")
                finally:
                    release.set()
            assert (await turn.get_result()).final_output == "finished"
            observed = [event async for event in turn.events(include_execution_details=True)]
            previews = [event for event in observed if isinstance(event, ReasoningSummaryTextDelta)]
            assert [event.delta for event in previews] == ([summary] if capture else [])
            assert not any(isinstance(event, RawProviderEvent) for event in observed)
            assert private not in repr(observed)
            history = str([item async for item in session.items()])
            assert summary in history
            assert private not in history


@pytest.mark.parametrize("capture", [False, True])
async def test_argument_previews_require_raw_capture_and_detailed_reader(responses, capture):
    effects = []

    @function_tool
    def record(value: str) -> str:
        effects.append(value)
        return "recorded"

    done = function_call("record", {"value": "accepted argument"}, "call-preview")
    done["item"]["id"] = "function-item"
    preview = {
        "type": "response.function_call_arguments.delta",
        "item_id": "function-item",
        "output_index": 0,
        "delta": '{"value":"unaccepted preview"}',
    }
    responses.respond(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**done["item"], "arguments": ""},
        },
        preview,
        {**preview, "output_index": True, "delta": "malformed preview"},
        done,
    )
    responses.respond(assistant_output("finished"))
    async with (
        timeout(15),
        NativeProvider(base_url=responses.url, api_key="synthetic-key") as provider,
    ):
        async with Agent(instructions="Record.", tools=[record], provider=provider).session(
            include_raw_provider_events=capture, observation_retention="all"
        ) as session:
            turn = await session.submit("Record.")
            assert (await turn.get_result()).final_output == "finished"
            assert effects == ["accepted argument"]
            ordinary = [event async for event in turn.events()]
            assert not any(isinstance(event, FunctionCallArgumentsDelta) for event in ordinary)
            detailed = [event async for event in turn.events(include_execution_details=True)]
            previews = [
                event for event in detailed if isinstance(event, FunctionCallArgumentsDelta)
            ]
            assert [event.delta for event in previews] == ([preview["delta"]] if capture else [])
            if capture:
                assert previews[0].turn_id == turn.turn_id
                assert previews[0].submission_id == turn.submission_id
                assert previews[0].item_id == "function-item"
                assert previews[0].output_index == 0
            assert "unaccepted preview" not in repr(detailed)
            history = str([item async for item in session.items()])
            assert "accepted argument" in history
            assert "unaccepted preview" not in history
