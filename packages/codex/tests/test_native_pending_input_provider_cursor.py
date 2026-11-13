"""Staged manual input uses the admitted provider cursor exactly once."""

import json

import pytest

from agents import Agent, FunctionTool, SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_provider_lineage import history_peer


@pytest.mark.parametrize("kind", ["previous_response", "conversation"])
@pytest.mark.parametrize("clear", [False, True])
async def test_staged_manual_input_keeps_provider_cursor_and_occurrences(kind, clear):
    lookup = FunctionTool(
        name="lookup", description="Read a record.", input_type=LookupArgs, output_type=str
    )
    selection = {"type": kind}
    if kind == "conversation":
        selection["id"] = "conv_current"
    async with timeout(30), history_peer() as (provider, model, reads, _, _wire):
        model.respond(function_call("lookup", {"key": "record"}, "manual-cursor"))
        model.respond(assistant_output("resumed"))
        model.respond(assistant_output("later"))
        agent = Agent(
            instructions="Read and continue.",
            model="gpt-5.5",
            provider=provider,
            tools=[lookup],
            model_settings={"store": True},
        )
        async with agent.session(provider_history=selection) as source:
            await source.submit("initial input")
            await next_request(source.interactions(), lookup)
            saved = await source.suspend()
        item = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "DUPLICATE_STAGED_OCCURRENCE"}],
        }
        staged = saved.with_pending_input([item, item])
        assert saved.pending_input == []
        assert staged.pending_input == [item, item]
        detached = staged.pending_input
        detached[0]["role"] = "assistant"
        assert staged.pending_input == [item, item]
        if clear:
            staged = staged.without_pending_input()
        restored_state = SessionState.from_json(staged.to_json())
        assert restored_state.pending_input == ([] if clear else [item, item])
        authorized = {
            "type": kind,
            "id": "resp_1" if kind == "previous_response" else "conv_current",
        }
        async with agent.session(state=restored_state, provider_history=authorized) as restored:
            request = await next_request(restored.interactions(), lookup)
            await request.respond("accepted manual result")
            assert (await restored.restored_turn.get_result()).final_output == "resumed"
            assert (
                await (await restored.submit("later input")).get_result()
            ).final_output == "later"
        assert len(model.requests) == 3
        resumed = model.requests[1]
        pending_indexes = [
            index
            for index, value in enumerate(resumed["input"])
            if "DUPLICATE_STAGED_OCCURRENCE" in json.dumps(value)
        ]
        assert len(pending_indexes) == (0 if clear else 2)
        output_indexes = [
            index
            for index, value in enumerate(resumed["input"])
            if value.get("type") == "function_call_output"
            and value.get("call_id") == "manual-cursor"
        ]
        assert len(output_indexes) == 1
        assert clear or max(output_indexes) < min(pending_indexes)
        assert "DUPLICATE_STAGED_OCCURRENCE" not in json.dumps(model.requests[2]["input"])
        assert "initial input" not in json.dumps(resumed["input"])
        if kind == "previous_response":
            assert resumed["previous_response_id"] == "resp_1"
            assert model.requests[2]["previous_response_id"] == "resp_2"
            assert ("/v1/responses/resp_1", "Bearer current-owner") in reads
        else:
            assert resumed["conversation"] == "conv_current"
            assert model.requests[2]["conversation"] == "conv_current"
            assert ("/v1/conversations/conv_current", "Bearer current-owner") in reads
