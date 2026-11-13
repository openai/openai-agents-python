import json
from dataclasses import replace

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    FunctionTool,
    Handoff,
    HandoffInput,
    NativeError,
    NativeProvider,
    PendingRequests,
    SessionState,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)


class Empty(BaseModel):
    pass


async def test_handoff_input_restores_presentation_after_settled_sibling_effects(
    responses,
):
    application = {"sibling": False}
    effects = []
    preparations = []

    @function_tool
    def sibling() -> str:
        application["sibling"] = True
        effects.append("sibling")
        return "sibling durable output"

    def prepare(value: HandoffInput[dict[str, bool]]) -> HandoffInput[dict[str, bool]]:
        assert value.target is target
        assert value.context is application
        assert value.context["sibling"] is True
        assert "sibling durable output" in json.dumps(value.generated)
        assert "source-private" in json.dumps(value.input)
        preparations.append("prepared")
        return replace(
            value,
            input=(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "settled sibling summary"}],
                },
            ),
            previous=(),
            generated=(),
        )

    confirm = FunctionTool(
        name="confirm", description="Confirm the destination.", input_type=Empty, output_type=str
    )
    target = Agent(name="B", instructions="DESTINATION instructions.", tools=[confirm])
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-handoff-input"
    ) as provider:
        source = Agent(
            name="A",
            instructions="SOURCE instructions.",
            provider=provider,
            tools=[sibling],
            handoffs=[target],
            prepare_handoff_input=prepare,
        )
        responses.respond(
            function_call("transfer_to_b", {}, "handoff-call"),
            function_call("sibling", {}, "sibling-call"),
        )
        responses.respond(function_call("confirm", {}, "destination-call"))
        responses.respond(assistant_output("destination complete"))
        async with timeout(30), source.session(context=application) as session:
            turn = await session.submit("source-private")
            pending = await turn.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            assert await session.get_active_agent() is target
            saved = await session.suspend()
        assert effects == ["sibling"]
        assert preparations == ["prepared"]
        assert "source-private" in saved.to_json()
        assert "sibling durable output" in saved.to_json()
        history = json.loads(saved.to_json())["history"]
        canonical = [row["payload"] for row in history if row["type"] == "response_item"]
        sibling_outputs = [
            item
            for item in canonical
            if item.get("type") == "function_call_output" and item.get("call_id") == "sibling-call"
        ]
        assert len(sibling_outputs) == 1
        assert "sibling durable output" in json.dumps(sibling_outputs[0])
        assert "source-private" in json.dumps(canonical)
        assert "settled sibling summary" not in json.dumps(canonical)
        admitted = [
            row.get("metadata", {}).get("sdk_agent_profile", {}).get("presentation")
            for row in history
            if row["type"] == "response_item"
        ]
        assert sum(snapshot is not None for snapshot in admitted) == 1
        assert "settled sibling summary" in json.dumps(
            next(snapshot for snapshot in admitted if snapshot is not None)
        )
        target_input = json.dumps(responses.requests[1]["input"])
        assert "settled sibling summary" in target_input
        assert "source-private" not in target_input
        assert "sibling durable output" not in target_input
        async with source.session(
            context=application, state=SessionState.from_json(saved.to_json())
        ) as restored:
            pending = await restored.restored_turn.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            matched = confirm.match(pending.requests[0])
            assert matched is not None
            await restored.respond(matched.reply("confirmed once"))
            assert (
                await restored.restored_turn.get_result()
            ).final_output == "destination complete"
        assert preparations == ["prepared"]
        assert tool_output(responses.requests[2], "destination-call") == "confirmed once"
        for kind in ("function_call", "function_call_output"):
            assert (
                sum(
                    item.get("type") == kind and item.get("call_id") == "destination-call"
                    for item in responses.requests[2]["input"]
                )
                == 1
            )
        assert "settled sibling summary" in json.dumps(responses.requests[2]["input"])
        assert "source-private" not in json.dumps(responses.requests[2]["input"])
        assert len(responses.requests) == 3


async def test_invalid_handoff_input_rejects_before_destination_sampling(responses):
    entered = []

    def invalid(value: HandoffInput[None]) -> HandoffInput[None]:
        entered.append("invalid projection")
        return replace(value, input=({"type": "message", "role": "developer", "content": []},))

    target = Agent(name="B", instructions="Destination.")
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-invalid-handoff"
    ) as provider:
        source = Agent(
            name="A",
            instructions="Source.",
            provider=provider,
            handoffs=[target],
            prepare_handoff_input=invalid,
        )
        responses.respond(function_call("transfer_to_b", {}, "handoff-call"))
        async with timeout(30), source.session() as session:
            with pytest.raises(NativeError):
                await (await session.submit("begin")).get_result()
            assert await session.get_active_agent() is source
        assert entered == ["invalid projection"]
        assert len(responses.requests) == 1


@pytest.mark.parametrize("mode", ["prepared", "old-filter", "old-plain"])
async def test_pending_handoff_restores_groups_and_blocks_old_receipts_only_when_preparing(
    responses, mode
):
    prepare_enabled = mode != "old-plain"
    effects = []
    preparations = []

    def prepare(value: HandoffInput[None]) -> HandoffInput[None]:
        assert "gate settled" in json.dumps(value.generated)
        preparations.append("prepared")
        return value

    gate = FunctionTool(
        name="gate", description="Settle sibling.", input_type=Empty, output_type=str
    )
    target = Agent(name="B", instructions="Destination.")
    async with NativeProvider(base_url=responses.url, api_key="synthetic-old-frontier") as provider:
        source = Agent(
            name="A",
            instructions="Source.",
            provider=provider,
            tools=[gate],
            handoffs=[target],
            prepare_handoff_input=prepare,
        )
        responses.respond(
            function_call("transfer_to_b", {}, "handoff-call"),
            function_call("gate", {}, "gate-call"),
        )
        async with timeout(30), source.session() as session:
            turn = await session.submit("source-private")
            pending = await turn.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            saved = await session.suspend()
        value = json.loads(saved.to_json())
        removed = 0
        for row in value["history"]:
            receipt = row.get("payload", {}).get("sdk_receipt")
            if isinstance(receipt, dict) and "handoff_input_frontiers" in receipt:
                if mode != "prepared":
                    receipt.pop("handoff_input_frontiers")
                removed += 1
        assert removed > 0
        checkpoint = SessionState.from_json(json.dumps(value))
        ordinary_or_filtered = replace(
            source, prepare_handoff_input=prepare if prepare_enabled else None
        )
        if mode != "old-filter":
            responses.respond(assistant_output("ordinary destination complete"))
        async with ordinary_or_filtered.session(state=checkpoint) as restored:
            pending = await restored.restored_turn.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            current = gate.match(pending.requests[0])
            assert current is not None
            await restored.respond(current.reply("gate settled"))
            if mode == "old-filter":
                with pytest.raises(NativeError):
                    await restored.restored_turn.get_result()
                assert await restored.get_active_agent() is ordinary_or_filtered
            else:
                assert (
                    await restored.restored_turn.get_result()
                ).final_output == "ordinary destination complete"
                assert await restored.get_active_agent() is target
        assert effects == []
        assert preparations == (["prepared"] if mode == "prepared" else [])
        assert len(responses.requests) == (1 if mode == "old-filter" else 2)


async def test_callback_backed_live_preparation_preserves_restored_acceptance_denial(responses):
    effects = []
    preparations = []

    @function_tool
    def route() -> str:
        effects.append("original route")
        return "handoff accepted"

    def prepare(value: HandoffInput[None]) -> HandoffInput[None]:
        assert value.target is target
        preparations.append("prepared")
        return value

    confirm = FunctionTool(
        name="confirm", description="Confirm.", input_type=Empty, output_type=str
    )
    target = Agent(name="B", instructions="Destination.", tools=[confirm])
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-callback-denial"
    ) as provider:
        source = Agent(
            name="A",
            instructions="Source.",
            provider=provider,
            handoffs=[Handoff(target, route)],
            prepare_handoff_input=prepare,
        )
        responses.respond(function_call("route", {}, "handoff-call"))
        responses.respond(function_call("confirm", {}, "destination-call"))
        async with timeout(30), source.session() as session:
            turn = await session.submit("begin")
            pending = await turn.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            assert await session.get_active_agent() is target
            saved = await session.suspend()
        assert effects == ["original route"]
        assert preparations == ["prepared"]
        with pytest.raises(Exception, match="live (callback )?acceptance"):
            async with source.session(state=saved):
                raise AssertionError("Restored callback authority was admitted")
        assert effects == ["original route"]
        assert preparations == ["prepared"]
        assert len(responses.requests) == 2
