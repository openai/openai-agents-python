import json
from dataclasses import replace
from hashlib import sha256

import pytest

from agents import (
    Agent,
    FunctionTool,
    HandoffInput,
    NativeProvider,
    PendingRequests,
    SessionState,
    function_tool,
    nest_handoff_history,
    remove_all_tools,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_handoff_input import Empty


def test_remove_all_tools_preserves_retained_summary_source_indices():
    target = Agent(instructions="Continue.")
    message = {"type": "message", "role": "user", "content": "ORIGINAL_TEXT"}
    result = {"type": "function_call_output", "call_id": "original", "output": "HIDDEN_TOOL_RESULT"}
    value = HandoffInput((message, result), (), (), target=target, recorded_generated=())
    nested = nest_handoff_history(remove_all_tools(value))
    calls = {"type": "function_call", "name": "before_summary", "call_id": "other"}
    from _agents_codex.handoff_history import _bind_handoff_summaries

    moved = replace(nested, input=(calls, *nested.input))
    _bind_handoff_summaries(
        moved, [{"index": 1, "wrappers": ["<CONVERSATION HISTORY>", "</CONVERSATION HISTORY>"]}]
    )
    clean = remove_all_tools(moved)
    assert clean.input == nested.input
    assert clean.target is target
    assert clean.recorded_generated == ()
    seen = []
    nest_handoff_history(clean, history_mapper=lambda records: seen.extend(records) or records)
    assert seen == [{"role": "user", "content": "ORIGINAL_TEXT"}]


@pytest.mark.asyncio
async def test_native_handoff_tool_filter_redacts_presentation_only(responses):
    effects = []
    prepares = []

    @function_tool
    def sibling() -> str:
        effects.append("ran")
        return "SIBLING_PRIVATE_RESULT"

    confirm = FunctionTool(
        name="confirm", description="Confirm the transfer.", input_type=Empty, output_type=str
    )
    target = Agent(name="B", instructions="DESTINATION instructions.", tools=[confirm])

    def filter_handoff(data):
        prepares.append(data.target)
        assert data.target is target
        assert "SIBLING_PRIVATE_RESULT" in json.dumps(data.generated)
        return remove_all_tools(data)

    async with (
        timeout(20),
        NativeProvider(base_url=responses.url, api_key="synthetic-native-filter") as provider,
    ):
        source = Agent(
            name="A",
            instructions="SOURCE instructions.",
            provider=provider,
            tools=[sibling],
            handoffs=[target],
            prepare_handoff_input=filter_handoff,
        )
        responses.respond(
            function_call("transfer_to_b", {}, "transfer"),
            function_call("sibling", {}, "sibling"),
        )
        responses.respond(function_call("confirm", {}, "target-confirm"))
        responses.respond(assistant_output("complete"))
        async with source.session() as original:
            first = await original.submit("VISIBLE_USER_TEXT")
            assert isinstance(await first.wait_for_request_or_result(), PendingRequests)
            encoded = (await original.suspend()).to_json()
            saved = SessionState.from_json(
                encoded, expected_sha256=sha256(encoded.encode()).hexdigest()
            )
        assert "SIBLING_PRIVATE_RESULT" in saved.to_json()
        assert "VISIBLE_USER_TEXT" in saved.to_json()
        async with source.session(state=saved) as resumed:
            [confirmation] = await resumed.pending_interactions()
            assert confirmation.call_id == "target-confirm"
            await confirmation.respond("accepted")
            assert resumed.restored_turn is not None
            assert (await resumed.restored_turn.get_result()).final_output == "complete"
        assert effects == ["ran"]
        assert prepares == [target]
        for request in responses.requests[1:]:
            assert "VISIBLE_USER_TEXT" in json.dumps(request["input"])
            assert "SIBLING_PRIVATE_RESULT" not in json.dumps(request["input"])
            assert not any(
                item.get("call_id") in {"transfer", "sibling"} for item in request["input"]
            )
