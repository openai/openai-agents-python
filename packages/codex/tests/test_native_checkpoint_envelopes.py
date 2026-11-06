"""Checkpoint storage across the original and fingerprinted output contracts."""

import json

import pytest
from pydantic import ValidationError

from agents import (
    Agent,
    FunctionTool,
    NativeOutput,
    NativeProvider,
    PromptedOutput,
    function_tool,
)
from agents.state import SessionState
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import LookupArgs, next_request, provider


@pytest.mark.parametrize("output_type", [None, int])
async def test_pre_output_idle_envelope_starts_a_new_turn(responses: ResponsesFixture, output_type):
    # The original format-2 native export has neither SDK output field.
    responses.respond(assistant_output("remembered" if output_type is None else '{"response":8}'))
    responses.respond(assistant_output("next" if output_type is None else '{"response":19}'))
    agent = Agent(instructions="Answer.", provider=provider(responses), output_type=output_type)
    async with agent.session() as session:
        result = await (await session.submit("first")).get_result()
        assert result.final_output == ("remembered" if output_type is None else 8)
        raw = json.loads((await session.save_state()).to_json())
    raw["format_version"] = 2
    raw.pop("output_version")
    raw.pop("output_fingerprint")
    saved = SessionState.from_json(json.dumps(raw))
    async with agent.session(state=saved) as restored:
        assert restored.id == saved.source_session_id
        assert restored.active_turn is None and restored.restored_turn is None
        result = await (await restored.submit("next")).get_result()
        assert result.final_output == ("next" if output_type is None else 19)
        assert (await restored.save_state()).format_version == 4
    assert len(responses.requests) == 2


async def test_pre_output_manual_envelope_rejects_new_modes_then_resumes_receipts(
    responses: ResponsesFixture,
):
    lookup = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    effects = []

    @function_tool
    async def record(key: str) -> str:
        effects.append(key)
        return "known-" + key

    agent = Agent(instructions="Read both.", tools=[lookup, record], provider=provider(responses))
    responses.respond(
        function_call("lookup", {"key": "A"}, "call-A"),
        function_call("record", {"key": "B"}, "call-B"),
    )
    async with agent.session() as source:
        await source.submit("read both")
        await next_request(source.interactions(), lookup)
        checkpoint = await source.suspend()
    assert effects == ["B"]
    raw = json.loads(checkpoint.to_json())
    raw["format_version"] = 3
    raw.pop("output_version")
    raw.pop("output_fingerprint")
    saved = SessionState.from_json(json.dumps(raw))
    unavailable = NativeProvider(base_url=responses.url, api_key="synthetic-closed")
    await unavailable.aclose()
    for mode in (NativeOutput(str), PromptedOutput(str)):
        refused = Agent(
            instructions=agent.instructions,
            tools=agent.tools,
            provider=unavailable,
            output_type=mode,
        )
        with pytest.raises(ValueError, match="output|schema"):
            async with refused.session(state=saved):
                pytest.fail("legacy continuation acquired fresh authority")
    raw["native_continuation"]["output_schema"] = {"type": "object", "properties": {}}
    refused = Agent(instructions=agent.instructions, tools=agent.tools, provider=unavailable)
    with pytest.raises(ValueError, match="native output schema does not match"):
        async with refused.session(state=SessionState.from_json(json.dumps(raw))):
            pytest.fail("native schema guard bypassed")
    assert len(responses.requests) == 1
    responses.respond(assistant_output("continued"))

    async with agent.session(state=saved) as restored:
        request = await next_request(restored.interactions(), lookup)
        assert request.call_id == "call-A" and request.arguments.key == "A"
        await request.respond("fresh-A")
        result = await restored.restored_turn.get_result()
        assert result.final_output == "continued"
        assert (await restored.save_state()).format_version == 4
    assert effects == ["B"]
    assert tool_output(responses.requests[-1], "call-A") == "fresh-A"
    assert tool_output(responses.requests[-1], "call-B") == "known-B"


@pytest.mark.parametrize("pending", [True, False], ids=["pending", "other-native-model"])
async def test_sdk_runner_refuses_incompatible_portable_state_before_open(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, pending: bool
) -> None:
    from agents.classic import Agent as ClassicAgent
    from agents.migration import SDKRunner
    from agents.migration._transport import _ResponseCarrier
    from tests.test_migration_runner import ScriptedModel, message

    lookup = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    agent = Agent(
        instructions="Read.",
        model="gpt-5.2",
        tools=[lookup] if pending else [],
        provider=provider(responses),
    )
    if pending:
        responses.respond(function_call("lookup", {"key": "A"}, "pending-call"))
    else:
        responses.respond(assistant_output("done"))
    async with agent.session() as source:
        turn = await source.submit("read")
        if pending:
            await next_request(source.interactions(), lookup)
            state = await source.suspend()
        else:
            await turn.get_result()
            state = await source.save_state()
    assert (state.native_continuation is not None) == pending

    async def forbidden(_carrier: _ResponseCarrier) -> None:
        pytest.fail("an incompatible checkpoint opened the carrier")

    monkeypatch.setattr(_ResponseCarrier, "start", forbidden)
    model = ScriptedModel([message("must not run")])
    match = "idle native state" if pending else "compatible native model profile"
    with pytest.raises(ValueError, match=match):
        await SDKRunner.run(ClassicAgent("Current", model=model), "continue", state=state)
    assert model.requests == []


@pytest.mark.parametrize("manual", [False, True])
async def test_fingerprinted_checkpoint_requires_complete_output_metadata_before_grants(
    responses: ResponsesFixture, manual: bool
):
    lookup = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    agent = Agent(instructions="Read.", tools=[lookup], provider=provider(responses))
    async with agent.session() as source:
        if manual:
            responses.respond(function_call("lookup", {"key": "A"}, "call-A"))
            await source.submit("read")
            await next_request(source.interactions(), lookup)
            saved = await source.suspend()
        else:
            responses.respond(assistant_output("saved"))
            await (await source.submit("read")).get_result()
            saved = await source.save_state()
    raw = json.loads(saved.to_json())
    assert raw["format_version"] == (5 if manual else 4)
    unavailable = NativeProvider(base_url=responses.url, api_key="synthetic-closed")
    await unavailable.aclose()
    refusing = Agent(instructions=agent.instructions, tools=agent.tools, provider=unavailable)
    for missing in (
        ("output_version",),
        ("output_fingerprint",),
        ("output_version", "output_fingerprint"),
    ):
        broken = raw.copy()
        for key in missing:
            del broken[key]
        with pytest.raises(ValidationError):
            SessionState.from_json(json.dumps(broken))
    for version, fingerprint in ((2, raw["output_fingerprint"]), (1, "changed")):
        with pytest.raises((ValidationError, ValueError)):
            broken = saved.model_copy(
                update={"output_version": version, "output_fingerprint": fingerprint}
            )
            async with refusing.session(state=broken):
                pytest.fail("invalid output metadata acquired the provider")
    assert len(responses.requests) == 1
