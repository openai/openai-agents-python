"""Parent event cursors observe real prompted-repair text before completion."""

from __future__ import annotations

import asyncio

import pytest

from agents import Agent, NativeProvider, PromptedOutput
from agents.observation import OutputTextDelta
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_agent_observation import text_response
from tests.test_native_prompted_repair import Report


@pytest.mark.parametrize("cursor", ["turn", "session"])
@pytest.mark.parametrize("attach", ["before-repair", "during-repair"])
async def test_parent_observation_demand_reaches_prompted_repair(responses, cursor, attach):
    original_waiting, release_original = asyncio.Event(), asyncio.Event()
    repair_started, release_text = asyncio.Event(), asyncio.Event()
    repair_waiting, release_repair = asyncio.Event(), asyncio.Event()
    invalid = assistant_output('{"label":"x"}')
    invalid["item"]["id"] = "initial-invalid"
    chunks = ('{"label":"', "cor", 'rected"}')
    corrected = "".join(chunks)

    async def before(event):
        if event["type"] == "response.output_text.delta" and event["delta"] == "cor":
            repair_started.set()
            await release_text.wait()
        if event["type"] != "response.output_item.done":
            return
        if event["item"].get("id") == "initial-invalid":
            original_waiting.set()
            await release_original.wait()
        elif event["item"].get("id") == "repair":
            # Every text fragment has been sent, but no item/response completion
            # can flush the coalescer or finish the repair while this is held.
            repair_waiting.set()
            await release_repair.wait()

    responses.before_event = before
    responses.respond(invalid)
    responses.respond(*text_response("repair", *chunks))
    provider = NativeProvider(
        base_url=responses.url,
        api_key="synthetic-repair-text-demand",
        supports_websockets=False,
        stream_idle_timeout_ms=10000,
        stream_max_retries=0,
        request_max_retries=0,
    )
    async with timeout(20), provider:
        agent = Agent(
            instructions="Return a report.",
            provider=provider,
            output_type=PromptedOutput(Report, 1),
        )
        async with agent.session() as session:
            reader = None
            running = None
            try:
                turn = await session.submit("Produce a report.")
                running = asyncio.create_task(turn.get_result())
                await original_waiting.wait()
                if attach == "before-repair":
                    reader = turn.events() if cursor == "turn" else session.events()
                release_original.set()
                await repair_started.wait()
                if attach == "during-repair":
                    reader = turn.events() if cursor == "turn" else session.events()
                release_text.set()
                await repair_waiting.wait()
                assert reader is not None
                seen = []
                async with timeout(2):
                    async for event in reader:
                        if (
                            isinstance(event, OutputTextDelta)
                            and event.parent_submission_id == turn.submission_id
                        ):
                            seen.append(event)
                            if "".join(item.delta for item in seen) == corrected:
                                break
                assert [event.delta for event in seen] == list(chunks)
                assert not running.done(), "Repair completion must still be held"
                assert all(event.session_id != session.id for event in seen)
                assert len({(event.session_id, event.turn_id) for event in seen}) == 1
                release_repair.set()
                result = await running
                assert result.final_output == Report(label="corrected")
                assert len(result.repair_attempts) == 1
                repair = result.repair_attempts[0]
                assert {(event.session_id, event.turn_id) for event in seen} == {
                    (repair["session_id"], repair["id"])
                }
            finally:
                release_original.set()
                release_text.set()
                release_repair.set()
                if reader is not None:
                    await reader.aclose()
                if running is not None:
                    if not running.done():
                        running.cancel()
                    await asyncio.gather(running, return_exceptions=True)
    assert len(responses.requests) == 2
