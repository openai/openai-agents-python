"""Selected staged inputs retain omitted occurrences in a native provider session."""

from __future__ import annotations

import asyncio
import json

import pytest

from agents import Agent, FunctionTool, SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_provider_lineage import history_peer

STAGED = "IDENTICAL_STAGED_ORIGINAL"


def staged_occurrences(items):
    return [item for item in items if STAGED in json.dumps(item)]


@pytest.mark.asyncio
@pytest.mark.parametrize("select_one", [False, True])
@pytest.mark.parametrize("history_kind", ["previous_response", "conversation"])
async def test_selected_staged_opaque_file_survives_next_manual_capture_and_cold_reopen(
    select_one, history_kind
):
    lookup = FunctionTool(
        name="lookup", description="Read a record.", input_type=LookupArgs, output_type=str
    )
    select = []

    def provider_history(response_id=None):
        if history_kind == "conversation":
            return {"type": "conversation", "id": "conv_current"}
        if response_id is None:
            return {"type": "previous_response"}
        return {"type": "previous_response", "id": response_id}

    def assert_cursor(request, response_id):
        if history_kind == "conversation":
            assert request["conversation"] == "conv_current"
            assert "previous_response_id" not in request
        else:
            assert request["previous_response_id"] == response_id
            assert "conversation" not in request

    def transform(request):
        matches = staged_occurrences(request.items)
        if select or len(matches) != 2:
            return None
        first, second = matches
        assert first is not second
        select.append(len(matches))
        return [item for item in request.items if item is not first]

    def agent_for(provider):
        return Agent(
            instructions="Read twice.",
            model="gpt-5.5",
            provider=provider,
            tools=[lookup],
            model_settings={"store": True},
            transform_context=transform if select_one else None,
        )

    async with timeout(25), history_peer() as (provider, model, reads, _, _):
        model.respond(function_call("lookup", {"key": "first"}, "manual-one"))
        model.respond(function_call("lookup", {"key": "second"}, "manual-two"))
        model.respond(assistant_output("complete"))
        agent = agent_for(provider)
        async with agent.session(provider_history=provider_history()) as original:
            await original.submit("Start.")
            first = await next_request(original.interactions(), lookup)
            assert first.call_id == "manual-one"
            state = await original.suspend()

        row = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_file", "file_id": STAGED, "filename": "user.pdf"}],
        }
        state = SessionState.from_json(state.with_pending_input([row, row]).to_json())
        async with agent.session(
            state=state, provider_history=provider_history("resp_1")
        ) as resumed:
            first = await next_request(resumed.interactions(), lookup)
            assert first.call_id == "manual-one"
            await first.respond("first accepted")
            assert resumed.restored_turn is not None
            next_tool = asyncio.create_task(next_request(resumed.interactions(), lookup))
            finished = asyncio.create_task(resumed.restored_turn.get_result())
            try:
                await asyncio.wait((next_tool, finished), return_when=asyncio.FIRST_COMPLETED)
                if finished.done():
                    finished.result()
                second = next_tool.result()
            finally:
                for task in (next_tool, finished):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(next_tool, finished, return_exceptions=True)
            assert second.call_id == "manual-two"
            saved = await resumed.suspend()
            assert saved.pending_input == ([row] if select_one else [])
            assert len(staged_occurrences(saved.history)) == (1 if select_one else 2)

        assert select == ([2] if select_one else [])
        assert len(staged_occurrences(model.requests[1]["input"])) == (1 if select_one else 2)
        assert_cursor(model.requests[1], "resp_1")
        saved = SessionState.from_json(saved.to_json())
        async with agent.session(
            state=saved, provider_history=provider_history("resp_2")
        ) as reopened:
            second = await next_request(reopened.interactions(), lookup)
            assert second.call_id == "manual-two"
            await second.respond("second accepted")
            assert reopened.restored_turn is not None
            assert (await reopened.restored_turn.get_result()).final_output == "complete"

    assert len(model.requests) == 3
    assert select == ([2] if select_one else [])
    for call_id in ("manual-one", "manual-two"):
        assert (
            sum(
                item.get("type") == "function_call_output" and item.get("call_id") == call_id
                for request in model.requests
                for item in request["input"]
            )
            == 1
        )
    assert_cursor(model.requests[2], "resp_2")
    assert len(staged_occurrences(model.requests[2]["input"])) == (1 if select_one else 0)
    if history_kind == "conversation":
        assert len(reads) >= 2
        assert all(
            read == ("/v1/conversations/conv_current", "Bearer current-owner") for read in reads
        )
    else:
        assert ("/v1/responses/resp_1", "Bearer current-owner") in reads
        assert ("/v1/responses/resp_2", "Bearer current-owner") in reads
