"""New hosted observations require explicit opt-in for provider payloads."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict

import pytest

from agents import Agent
from agents.observation import (
    HostedToolCallProgress,
    HostedToolCallSummary,
    ModelOutputItemCompleted,
    TurnFinished,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_hosted_tools import CASES, completed, provider


@pytest.mark.parametrize("reader", ["session", "turn"])
@pytest.mark.parametrize("tool,_wire,raw,_fields,_include", CASES)
async def test_hosted_payloads_require_reader_opt_in(
    responses, reader, tool, _wire, raw, _fields, _include
):
    raw = {**deepcopy(raw), "future_private": {"text": "synthetic-private-provider-data"}}
    progress_type = {
        "file_search_call": "response.file_search_call.searching",
        "code_interpreter_call": "response.code_interpreter_call_code.delta",
        "image_generation_call": "response.image_generation_call.partial_image",
    }[raw["type"]]
    progress = {
        "type": progress_type,
        "item_id": raw["id"],
        "output_index": 0,
        "sequence_number": 7,
        "future_private": {"text": "synthetic-private-progress-data"},
        **(
            {"delta": "print('synthetic-private-code')"}
            if raw["type"] == "code_interpreter_call"
            else {}
        ),
        **(
            {"partial_image_index": 0, "partial_image_b64": "A" * 150_000}
            if raw["type"] == "image_generation_call"
            else {}
        ),
    }
    responses.respond(progress, completed(raw), assistant_output("Done."))
    borrowed = provider(responses)
    async with (
        timeout(30),
        borrowed,
        Agent(
            instructions="Use the hosted tool.",
            model="gpt-5.2",
            provider=borrowed,
            tools=[tool],
        ).session() as session,
    ):
        turn = await session.submit("Perform the synthetic hosted task.")
        assert (await turn.get_result()).final_output == "Done."

        async def collect(**kwargs):
            cursor = session.events(**kwargs) if reader == "session" else turn.events(**kwargs)
            events = []
            async for event in cursor:
                events.append(event)
                if isinstance(event, TurnFinished) and event.turn_id == turn.turn_id:
                    break
            return events

        default = await collect()
        explicit_default = await collect(include_execution_details=False)
        detailed = await collect(include_execution_details=True)
        assert default == explicit_default
        summary = next(event for event in default if isinstance(event, ModelOutputItemCompleted))
        assert isinstance(summary.item, HostedToolCallSummary)
        assert type(summary.item).__module__ == "agents.observation"
        with pytest.raises(FrozenInstanceError):
            summary.item.status = "reader mutation"
        assert asdict(summary)["item"] == {
            "type": raw["type"],
            "id": raw["id"],
            "turn_id": turn.turn_id,
            "status": raw["status"],
        }
        safe_progress = next(
            event for event in default if isinstance(event, HostedToolCallProgress)
        )
        full_progress = next(
            event for event in detailed if isinstance(event, HostedToolCallProgress)
        )
        assert safe_progress.event == {"type": progress_type}
        assert asdict(safe_progress) == {**asdict(full_progress), "event": {"type": progress_type}}
        assert full_progress.event == progress
        full_item = next(
            event.item for event in detailed if isinstance(event, ModelOutputItemCompleted)
        )
        history = [item async for item in session.items() if item["type"] == raw["type"]]
        assert history == [full_item]
        assert history[0]["provider_data"] == raw
        assert "synthetic-private" not in str([asdict(event) for event in default])

        # Opt-in reader mutations must not modify retained events or canonical history.
        full_progress.event["future_private"] = "reader mutation"
        full_item["provider_data"] = {"reader": "mutation"}
        replay = await collect(include_execution_details=True)
        assert (
            next(event.event for event in replay if isinstance(event, HostedToolCallProgress))
            == progress
        )
        assert (
            next(event.item for event in replay if isinstance(event, ModelOutputItemCompleted))
            == history[0]
        )
        assert [item async for item in session.items() if item["type"] == raw["type"]] == history
    assert len(responses.requests) == 1
