import json

import pytest
from pydantic import BaseModel, ConfigDict

from agents import Agent, NativeOutput, NativeProvider
from tests.native_fixtures import assistant_output, function_call, responses as responses


class Shared(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str


class Source(BaseModel):
    source: bool


class Target(BaseModel):
    target: str


class Override(BaseModel):
    override: str


@pytest.mark.parametrize("source_strict", [True, False])
@pytest.mark.parametrize("shared_schema", [True, False])
async def test_profile_strictness_follows_destination_and_cold_restore(
    responses, source_strict, shared_schema
):
    expected = {"value": "complete"} if shared_schema else {"target": "complete"}
    responses.respond(function_call("transfer_to_b", {}, "strict-route"))
    responses.respond(assistant_output(json.dumps(expected)))
    responses.respond(assistant_output(json.dumps(expected)))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-output-strictness", supports_websockets=False
    ) as provider:
        destination = Agent(
            name="B",
            instructions="Complete.",
            output_type=NativeOutput(Shared if shared_schema else Target, strict=not source_strict),
        )
        source = Agent(
            name="A",
            model="gpt-5.5",
            provider=provider,
            instructions="Transfer.",
            output_type=NativeOutput(Shared if shared_schema else Source, strict=source_strict),
            handoffs=[destination],
        )
        async with source.session() as session:
            result = await (await session.submit("Route.")).get_result()
            assert result.final_output.model_dump() == expected
            state = await session.save_state()
        async with source.session(state=state) as restored:
            result = await (await restored.submit("Continue.")).get_result()
            assert result.final_output.model_dump() == expected
    formats = [request["text"]["format"] for request in responses.requests]
    assert formats[0]["strict"] is source_strict
    assert formats[1]["strict"] is not source_strict
    assert formats[2]["strict"] is not source_strict
    assert set(formats[1]["schema"]["properties"]) == set(expected)
    if shared_schema:
        assert formats[0]["schema"] == formats[1]["schema"]


@pytest.mark.parametrize("strict", [False, True])
async def test_explicit_output_strictness_retires_after_its_turn(responses, strict):
    responses.respond(function_call("transfer_to_b", {}, "override-route"))
    responses.respond(assistant_output('{"override":"complete"}'))
    responses.respond(assistant_output('{"target":"next"}'))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-output-override", supports_websockets=False
    ) as provider:
        destination = Agent(
            name="B",
            instructions="Complete.",
            output_type=NativeOutput(Target, strict=not strict),
        )
        source = Agent(
            name="A",
            model="gpt-5.5",
            provider=provider,
            instructions="Transfer.",
            output_type=Source,
            handoffs=[destination],
        )
        async with source.session() as session:
            turn = await session.submit("Route.", output_type=NativeOutput(Override, strict=strict))
            assert (await turn.get_result()).final_output.model_dump() == {"override": "complete"}
            turn = await session.submit("Next.")
            assert (await turn.get_result()).final_output.model_dump() == {"target": "next"}
    formats = [request["text"]["format"] for request in responses.requests]
    assert formats[0]["strict"] is strict
    assert formats[1]["strict"] is strict
    assert formats[2]["strict"] is not strict
    assert set(formats[2]["schema"]["properties"]) == {"target"}
