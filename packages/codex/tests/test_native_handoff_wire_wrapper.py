"""Handoff output wrappers follow the active native schema."""

import json

import pytest
from pydantic import BaseModel

from agents import Agent, NativeOutput, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses


class Record(BaseModel):
    count: int


@pytest.mark.parametrize("wrapped_destination", [True, False])
async def test_native_handoff_changes_output_wrapper_and_cold_restore(
    responses, wrapped_destination
):
    source_type = Record if wrapped_destination else int
    target_type = int if wrapped_destination else Record
    target_field = "response" if wrapped_destination else "count"
    source_field = "count" if wrapped_destination else "response"
    expected = 7 if wrapped_destination else Record(count=7)
    responses.respond(function_call("transfer_to_target", {}, "route-wrapper"))
    responses.respond(assistant_output(json.dumps({target_field: 7})))
    responses.respond(assistant_output(json.dumps({target_field: 7})))

    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-wire", supports_websockets=False
    ) as provider:
        target = Agent(name="target", instructions="Target.", output_type=NativeOutput(target_type))
        source = Agent(
            model="gpt-5.5",
            provider=provider,
            instructions="Source.",
            output_type=NativeOutput(source_type),
            handoffs=[target],
        )
        async with timeout(30):
            async with source.session() as session:
                result = await (await session.submit("Route.")).get_result()
                assert result.final_output == expected
                saved = await session.save_state()
            async with source.session(state=saved) as restored:
                assert await restored.get_active_agent() is target
                result = await (await restored.submit("Continue.")).get_result()
                assert result.final_output == expected

    formats = [request["text"]["format"]["schema"]["properties"] for request in responses.requests]
    assert [set(properties) for properties in formats] == [
        {source_field},
        {target_field},
        {target_field},
    ]
