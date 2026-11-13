"""Native handoffs scope prompted output guidance to the active profile."""

import json

import pytest
from pydantic import BaseModel

from agents import Agent, FunctionTool, NativeOutput, NativeProvider, PromptedOutput
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import next_request
from tests.test_native_provider_lineage import history_peer


class PromptedDestination(BaseModel):
    prompted_destination: int


class PromptedSource(BaseModel):
    prompted_source: int


@pytest.mark.parametrize("native_source", [False, True])
@pytest.mark.parametrize("override", [False, True])
async def test_prompted_handoff_replaces_wire_guidance(responses, native_source, override):
    source_output = (
        NativeOutput(PromptedSource) if native_source else PromptedOutput(PromptedSource)
    )
    responses.respond(function_call("transfer_to_target", {}, "prompted-route"))
    responses.respond(assistant_output("override" if override else '{"prompted_destination":7}'))
    responses.respond(assistant_output('{"prompted_destination":8}'))
    async with (
        timeout(20),
        NativeProvider(
            base_url=responses.url, api_key="synthetic-prompted", supports_websockets=False
        ) as provider,
    ):
        target = Agent(
            name="target", instructions="Target.", output_type=PromptedOutput(PromptedDestination)
        )
        agent = Agent(
            instructions="Source.",
            provider=provider,
            output_type=source_output,
            handoffs=[target],
        )
        async with agent.session() as source:
            turn = await source.submit("Route.", output_type=str if override else None)
            result = await turn.get_result()
            assert result.final_output == (
                "override" if override else PromptedDestination(prompted_destination=7)
            )
            saved = await source.save_state()
        async with agent.session(state=saved) as restored:
            assert await restored.get_active_agent() is target
            result = await (await restored.submit("Continue.")).get_result()
            assert result.final_output == PromptedDestination(prompted_destination=8)

    first, transferred, restored = responses.requests
    for req in (transferred, restored):
        assert req.get("text", {}).get("format", {}).get("type") != "json_schema"
        wire = json.dumps(req)
        assert "prompted_source" not in wire
        if req is restored or not override:
            assert "Return only JSON" in wire and "prompted_destination" in wire
        else:
            assert "Return only JSON" not in wire
    assert ("prompted_source" in json.dumps(first)) is not override
    assert (first.get("text", {}).get("format", {}).get("type") == "json_schema") is (
        native_source and not override
    )


async def test_prompted_handoff_scopes_guidance_to_manual_continuation(responses):
    lookup = FunctionTool(
        name="lookup", description="Read.", input_type=PromptedSource, output_type=str
    )
    responses.respond(function_call("transfer_to_target", {}, "prompted-manual-route"))
    responses.respond(function_call("lookup", {"prompted_source": 4}, "prompted-manual"))
    responses.respond(assistant_output('{"prompted_destination":7}'))
    async with (
        timeout(20),
        NativeProvider(
            base_url=responses.url, api_key="synthetic-prompted-manual", supports_websockets=False
        ) as provider,
    ):
        target = Agent(
            name="target",
            instructions="Target.",
            output_type=PromptedOutput(PromptedDestination),
            tools=[lookup],
        )
        agent = Agent(instructions="Source.", provider=provider, handoffs=[target])
        async with agent.session() as source:
            await source.submit("Route.")
            assert (await next_request(source.interactions(), lookup)).call_id == "prompted-manual"
            saved = await source.suspend()
            assert saved.native_continuation is not None
            assert saved.native_continuation.output_schema is None
            assert "Return only JSON" not in saved.to_json()
        async with agent.session(state=saved) as restored:
            assert await restored.get_active_agent() is target
            request = await next_request(restored.interactions(), lookup)
            await request.respond("settled once")
            assert restored.restored_turn is not None
            result = await restored.restored_turn.get_result()
            assert result.final_output == PromptedDestination(prompted_destination=7)
    first, transferred, continued = responses.requests
    assert "Return only JSON" not in json.dumps(first)
    for request in (transferred, continued):
        assert "Return only JSON" in json.dumps(request)
        assert "prompted_destination" in json.dumps(request)
    assert tool_output(continued, "prompted-manual") == "settled once"


async def test_prompted_handoff_guidance_preserves_provider_history_cursor():
    async with timeout(20), history_peer() as (provider, model, _reads, _base, _wire):
        model.respond(function_call("transfer_to_target", {}, "prompted-stored"))
        model.respond(assistant_output('{"prompted_destination":7}'))
        target = Agent(
            name="target",
            instructions="Target.",
            model_settings={"store": True},
            output_type=PromptedOutput(PromptedDestination),
        )
        source = Agent(
            instructions="Source.",
            model="gpt-5.5",
            model_settings={"store": True},
            provider=provider,
            output_type=PromptedOutput(PromptedSource),
            handoffs=[target],
        )
        async with source.session(provider_history={"type": "previous_response"}) as session:
            result = await (await session.submit("Route.")).get_result()
            assert result.final_output == PromptedDestination(prompted_destination=7)
            assert (await session.save_state()).provider_history == {
                "type": "previous_response",
                "id": "resp_2",
            }
        first, second = model.requests
        assert "prompted_source" in json.dumps(first["input"])
        assert second["previous_response_id"] == "resp_1"
        assert "prompted_source" not in json.dumps(second["input"])
        assert "prompted_destination" in json.dumps(second["input"])
