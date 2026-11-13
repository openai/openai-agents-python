import json

import pytest
from pydantic import BaseModel

from agents import Agent, FunctionTool, NativeOutput, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_manual_tools import next_request


@pytest.mark.parametrize("override", ["none", "different", "source-equivalent"])
async def test_native_destination_wire_schema_requires_profile_settings_seam(responses, override):
    class SourceValue(BaseModel):
        source_value: str

    class TargetValue(BaseModel):
        target_value: str

    class OverrideValue(BaseModel):
        override_value: str

    responses.respond(function_call("transfer_to_target", {}, "route"))
    responses.respond(
        assistant_output(
            json.dumps(
                {"override_value": "ok"}
                if override == "different"
                else (
                    {"source_value": "ok"}
                    if override == "source-equivalent"
                    else {"target_value": "ok"}
                )
            )
        )
    )
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-wire", supports_websockets=False
    ) as provider:
        target = Agent(name="target", instructions="Target.", output_type=NativeOutput(TargetValue))
        source = Agent(
            name="source",
            model="gpt-5.5",
            provider=provider,
            instructions="Source.",
            output_type=NativeOutput(SourceValue),
            handoffs=[target],
        )
        async with timeout(30), source.session() as session:
            turn = await session.submit(
                "Route.",
                **(
                    {
                        "output_type": NativeOutput(
                            OverrideValue if override == "different" else SourceValue
                        )
                    }
                    if override != "none"
                    else {}
                ),
            )
            result = await turn.get_result()
            assert isinstance(
                result.final_output,
                OverrideValue
                if override == "different"
                else (SourceValue if override == "source-equivalent" else TargetValue),
            )
            assert await session.get_active_agent() is target
    first = responses.requests[0]["text"]["format"]["schema"]["properties"]
    second = responses.requests[1]["text"]["format"]["schema"]["properties"]
    assert set(first) == ({"override_value"} if override == "different" else {"source_value"})
    assert set(second) == (
        {"override_value"}
        if override == "different"
        else ({"source_value"} if override == "source-equivalent" else {"target_value"})
    )


async def test_distinct_wire_schema_survives_idle_save_restore(responses):
    class SourceValue(BaseModel):
        source_value: str

    class TargetValue(BaseModel):
        target_value: str

    responses.respond(function_call("transfer_to_target", {}, "idle-route"))
    responses.respond(assistant_output('{"target_value":"first"}'))
    responses.respond(assistant_output('{"target_value":"restored"}'))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-wire", supports_websockets=False
    ) as provider:
        target = Agent(name="target", instructions="Target.", output_type=NativeOutput(TargetValue))
        source = Agent(
            model="gpt-5.5",
            provider=provider,
            instructions="Source.",
            output_type=NativeOutput(SourceValue),
            handoffs=[target],
        )
        async with timeout(30):
            async with source.session() as session:
                result = await (await session.submit("Route.")).get_result()
                assert isinstance(result.final_output, TargetValue)
                saved = await session.save_state()
            async with source.session(state=saved) as restored:
                assert await restored.get_active_agent() is target
                result = await (await restored.submit("Continue.")).get_result()
                assert isinstance(result.final_output, TargetValue)
                assert result.final_output.target_value == "restored"
    assert len(responses.requests) == 3
    assert set(responses.requests[2]["text"]["format"]["schema"]["properties"]) == {"target_value"}


async def test_distinct_wire_pending_refusal_keeps_live_receipt_usable(responses):
    class SourceValue(BaseModel):
        source_value: str

    class TargetValue(BaseModel):
        target_value: str

    class DecisionArgs(BaseModel):
        key: str

    decision = FunctionTool(
        name="decision", description="Ask.", input_type=DecisionArgs, output_type=str
    )
    responses.respond(function_call("decision", {"key": "one"}, "pending-decision"))
    responses.respond(assistant_output('{"source_value":"continued"}'))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-wire", supports_websockets=False
    ) as provider:
        target = Agent(name="target", instructions="Target.", output_type=NativeOutput(TargetValue))
        source = Agent(
            model="gpt-5.5",
            provider=provider,
            instructions="Source.",
            tools=[decision],
            output_type=NativeOutput(SourceValue),
            handoffs=[target],
        )
        async with timeout(30), source.session() as session:
            turn = await session.submit("Ask then finish.")
            request = await next_request(session.interactions(), decision)
            assert len(responses.requests) == 1
            with pytest.raises(ValueError, match="Per-profile output schemas.*pending checkpoints"):
                await session.suspend()
            assert len(responses.requests) == 1
            assert [item.call_id for item in await session.pending_interactions()] == [
                request.call_id
            ]
            await request.respond("settled-manual")
            result = await turn.get_result()
            assert isinstance(result.final_output, SourceValue)
            assert result.final_output.source_value == "continued"
    assert len(responses.requests) == 2
    assert json.dumps(responses.requests[1]["input"]).count("settled-manual") == 1
    assert set(responses.requests[1]["text"]["format"]["schema"]["properties"]) == {"source_value"}
