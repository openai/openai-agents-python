from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agents import Agent, HandoffInput
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_provider_websockets import peer


@pytest.mark.parametrize("distinct", [False, True], ids=["shared-provider", "distinct-provider"])
async def test_prepared_handoff_omits_source_developer_instructions(distinct: bool) -> None:
    source_marker = "source-only instruction marker"
    destination_marker = "destination instruction marker"
    updated_marker = "updated destination instruction marker"
    source_input = "private source input"
    retained_input = "approved handoff summary"

    def prepare(value: HandoffInput[None]) -> HandoffInput[None]:
        return replace(
            value,
            input=(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": retained_input}],
                },
            ),
            previous=(),
            generated=(),
        )

    async with timeout(30), peer() as source, peer() as other:
        target = other if distinct else source
        source.respond(function_call("transfer_to_destination", {}, "redacted-handoff"))
        target.respond(assistant_output("destination reached"))
        target.respond(assistant_output("destination continued"))
        source_provider = source.provider(websocket=False, key="source-handoff-test")
        target_provider = (
            target.provider(websocket=False, key="target-handoff-test")
            if distinct
            else source_provider
        )
        destination = Agent(
            name="Destination",
            model="gpt-5.5",
            instructions=destination_marker,
            provider=target_provider,
        )
        agent = Agent(
            model="gpt-5.5",
            instructions=source_marker,
            provider=source_provider,
            handoffs=[destination],
            prepare_handoff_input=prepare,
        )
        async with agent.session() as session:
            result = await (await session.submit(source_input)).get_result()
            assert result.final_output == "destination reached"
            await session.update(instructions=updated_marker)
            result = await (await session.submit("Follow up.")).get_result()
            assert result.final_output == "destination continued"
            saved = (await session.save_state()).to_json()

        assert source_marker in json.dumps(source.requests[0]["input"])
        first_target, follow_up = [
            json.dumps(request["input"]) for request in target.requests[-2:]
        ]
        assert destination_marker in first_target
        assert updated_marker in follow_up
        for wire in (first_target, follow_up):
            assert retained_input in wire
            assert source_input not in wire
            assert source_marker not in wire
        assert source_marker in saved
