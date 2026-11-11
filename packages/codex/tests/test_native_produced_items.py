from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from agents import (
    ActionReply,
    Agent,
    FunctionTool,
    Interaction,
    InteractionRequired,
    NativeError,
    NativeProvider,
    ProducedItem,
    function_tool,
)
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_manual_tools import LookupArgs


async def test_one_shot_items_with_unused_manual_tool(responses):
    lookup = FunctionTool(
        name="lookup", description="Look up a record.", input_type=LookupArgs, output_type=str
    )
    responses.respond(assistant_output("manual reply was unnecessary"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-produced-items"
    ) as provider:
        agent = Agent(instructions="Use lookup if needed.", provider=provider, tools=[lookup])
        completed = await agent.run("answer directly", return_items=True)

    assert completed.final_output == "manual reply was unnecessary"
    assert completed.items is not None
    assert len(completed.items) == 1
    assert isinstance(completed.items[0], ProducedItem)
    assert completed.items[0].item["type"] == "message"
    assert completed.items[0].agent is agent


@pytest.mark.parametrize("delegated", [False, True])
async def test_one_shot_items_with_restored_manual_tool(responses, delegated):
    lookup = FunctionTool(
        name="lookup", description="Look up a record.", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "A"}, "restored-produced"))

    def answer(event: Interaction) -> ActionReply[str]:
        request = lookup.match(event)
        assert request is not None
        return request.reply("accepted-A")

    class DelegatingAgent(Agent):
        @asynccontextmanager
        async def session(self, **options: Any):
            async with super().session(**options) as session:
                if options.get("state") is not None:
                    restored = session.restored_turn
                    assert restored is not None
                    await restored.get_result()
                yield session

    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-produced-items"
    ) as provider:
        agent_type = DelegatingAgent if delegated else Agent
        agent = agent_type(instructions="Use lookup once.", provider=provider, tools=[lookup])
        with pytest.raises(InteractionRequired) as paused:
            await agent.run("look up A", return_items=True)
        saved = paused.value.state
        assert saved is not None and saved.native_continuation is not None
        responses.respond(assistant_output("finished"))
        completed = await agent.run(None, state=saved, on_interaction=answer, return_items=True)

    assert completed.final_output == "finished"
    assert completed.items is not None
    assert [record.item["type"] for record in completed.items] == [
        "function_call_output",
        "message",
    ]
    assert completed.items[0].item["output"] == "accepted-A"
    assert all(record.agent is agent for record in completed.items)
    assert len(responses.requests) == 2


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
