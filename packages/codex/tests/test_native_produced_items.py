from __future__ import annotations

import pytest

from agents import Agent, NativeError, NativeProvider, function_tool
from tests.native_fixtures import assistant_output, function_call, responses as responses


async def test_one_shot_items_keep_the_owned_tool_sequence_and_handoff_agents(responses):
    calls = []

    @function_tool
    def lookup(key: str) -> str:
        calls.append(key)
        return f"value:{key}"

    responses.respond(assistant_output("previous answer"))
    responses.respond(function_call("transfer_to_b", {}, "handoff-a-b"))
    responses.respond(function_call("lookup", {"key": "current"}, "lookup-current"))
    responses.respond(assistant_output("current answer"))

    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-produced-items"
    ) as provider:
        target = Agent(name="B", instructions="Use lookup.", tools=[lookup])
        source = Agent(
            name="A", instructions="Transfer to B.", handoffs=[target], provider=provider
        )
        first = await source.run("previous input", return_state=True)
        assert first.items is None
        assert first.state is not None

        completed = await source.run(
            "current input", state=first.state, return_history=True, return_items=True
        )

    assert completed.final_output == "current answer"
    assert completed.history is not None
    assert any(
        item["type"] == "message"
        and item.get("role") == "assistant"
        and any(part.get("text") == "previous answer" for part in item.get("content", ()))
        for item in completed.history
    )
    assert completed.items is not None
    assert [
        (
            produced.item["type"],
            produced.item.get("call_id"),
            produced.agent,
            produced.handoff_target,
        )
        for produced in completed.items
    ] == [
        ("function_call", "handoff-a-b", source, None),
        ("function_call_output", "handoff-a-b", source, target),
        ("function_call", "lookup-current", target, None),
        ("function_call_output", "lookup-current", target, None),
        ("message", None, target, None),
    ]
    assert calls == ["current"]
    assert len(responses.requests) == 4


async def test_one_shot_items_bound_original_tool_output_after_the_turn(responses):
    effects = []

    @function_tool
    def lookup() -> str:
        effects.append("executed")
        return "x" * (8 * 1024 * 1024)

    responses.respond(function_call("lookup", {}, "large-output"))
    responses.respond(assistant_output("finished"))

    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-produced-items"
    ) as provider:
        agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider)
        with pytest.raises(
            NativeError, match="Produced run items exceed the one-shot result limit"
        ):
            await agent.run("lookup", return_items=True)

    assert effects == ["executed"]
    assert len(responses.requests) == 2
