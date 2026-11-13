"""Native handoff output authority survives a cold manual continuation."""

import json

import pytest
from pydantic import BaseModel

from agents import Agent, FunctionTool, NativeOutput, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import next_request


class SourceValue(BaseModel):
    source_value: str


class TargetValue(BaseModel):
    target_value: str


class DecisionArgs(BaseModel):
    key: str


@pytest.mark.parametrize("explicit_override", [False, True])
async def test_manual_checkpoint_after_handoff_keeps_exact_wire_owner(responses, explicit_override):
    decision = FunctionTool(
        name="decision", description="Ask.", input_type=DecisionArgs, output_type=str
    )
    output_type = SourceValue if explicit_override else TargetValue
    field = "source_value" if explicit_override else "target_value"
    responses.respond(function_call("transfer_to_target", {}, "manual-route"))
    responses.respond(function_call("decision", {"key": "one"}, "pending-decision"))
    responses.respond(assistant_output(json.dumps({field: "continued"})))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-wire", supports_websockets=False
    ) as provider:
        target = Agent(
            name="target",
            instructions="Target.",
            tools=[decision],
            output_type=NativeOutput(TargetValue),
        )
        source = Agent(
            name="source",
            model="gpt-5.5",
            provider=provider,
            instructions="Source.",
            output_type=NativeOutput(SourceValue),
            handoffs=[target],
        )
        async with timeout(30):
            async with source.session() as session:
                await session.submit(
                    "Transfer, ask, and finish.",
                    output_type=NativeOutput(SourceValue) if explicit_override else None,
                )
                request = await next_request(session.interactions(), decision)
                assert request.call_id == "pending-decision"
                saved = await session.suspend()
            assert len(responses.requests) == 2
            assert saved.native_continuation.output_schema["properties"].keys() == {field}
            async with source.session(
                state=saved,
                restored_output_type=NativeOutput(SourceValue) if explicit_override else None,
            ) as restored:
                request = await next_request(restored.interactions(), decision)
                assert request.call_id == "pending-decision"
                await request.respond("settled-once")
                result = await restored.restored_turn.get_result()
                assert isinstance(result.final_output, output_type)
                assert result.final_output.model_dump() == {field: "continued"}
    assert len(responses.requests) == 3
    assert tool_output(responses.requests[2], "pending-decision") == "settled-once"
    for request in responses.requests[1:]:
        assert set(request["text"]["format"]["schema"]["properties"]) == {field}
