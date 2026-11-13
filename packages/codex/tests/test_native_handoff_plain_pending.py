"""A cold manual continuation preserves plain destination and override output."""

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


@pytest.mark.parametrize(
    "explicit_plain_override", [False, True], ids=["plain-destination", "explicit-plain-override"]
)
async def test_pending_handoff_retains_plain_wire_output(responses, explicit_plain_override):
    decision = FunctionTool(
        name="decision", description="Ask.", input_type=DecisionArgs, output_type=str
    )
    responses.respond(function_call("transfer_to_target", {}, "plain-route"))
    responses.respond(function_call("decision", {"key": "one"}, "pending-plain"))
    responses.respond(assistant_output("continued"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-plain-wire", supports_websockets=False
    ) as provider:
        target = Agent(
            name="target",
            instructions="Target.",
            tools=[decision],
            output_type=NativeOutput(TargetValue) if explicit_plain_override else str,
        )
        source = Agent(
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
                    output_type=str if explicit_plain_override else None,
                )
                pending = await next_request(session.interactions(), decision)
                assert pending.call_id == "pending-plain"
                saved = await session.suspend()
            assert len(responses.requests) == 2
            assert saved.native_continuation is not None
            assert saved.native_continuation.output_schema is None
            async with source.session(
                state=saved,
                restored_output_type=str if explicit_plain_override else None,
            ) as restored:
                assert await restored.get_active_agent() is target
                current = await next_request(restored.interactions(), decision)
                assert current.call_id == "pending-plain"
                await current.respond("settled-once")
                assert restored.restored_turn is not None
                result = await restored.restored_turn.get_result()
                assert result.final_output == "continued"
    assert len(responses.requests) == 3
    first_format = (responses.requests[0].get("text") or {}).get("format")
    assert (first_format is None) is explicit_plain_override
    for request in responses.requests[1:]:
        assert "format" not in (request.get("text") or {})
    assert tool_output(responses.requests[2], "pending-plain") == "settled-once"
