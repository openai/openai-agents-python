"""Selected staged inputs retain omitted occurrences in a native provider session."""

from __future__ import annotations

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
async def test_selected_staged_row_survives_next_manual_capture_and_cold_reopen(
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
            "content": [{"type": "input_text", "text": STAGED}],
        }
        state = SessionState.from_json(state.with_pending_input([row, row]).to_json())
        async with agent.session(
            state=state, provider_history=provider_history("resp_1")
        ) as resumed:
            first = await next_request(resumed.interactions(), lookup)
            assert first.call_id == "manual-one"
            await first.respond("first accepted")
            second = await next_request(resumed.interactions(), lookup)
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


@pytest.mark.asyncio
async def test_preterminal_transport_failure_keeps_both_staged_occurrences():
    from agents import NativeError

    lookup = FunctionTool(
        name="lookup", description="Read a record.", input_type=LookupArgs, output_type=str
    )

    def transform(request):
        matches = staged_occurrences(request.items)
        if len(matches) != 2:
            return None
        return [item for item in request.items if item is not matches[0]]

    async with timeout(25), history_peer() as (provider, model, _, _url, wire):
        model.respond(function_call("lookup", {"key": "first"}, "manual-one"))

        # Terminate the second physical stream after output but before its
        # native completion boundary. The first accepted response is unaffected.
        async def fail_second_completion(event):
            if len(model.requests) == 2 and event["type"] == "response.completed":
                raise ConnectionResetError("synthetic preterminal failure")

        model.before_event = fail_second_completion
        model.respond(assistant_output("incomplete result"))
        agent = Agent(
            instructions="Read twice.",
            model="gpt-5.5",
            provider=provider,
            tools=[lookup],
            model_settings={"store": True},
            transform_context=transform,
        )
        async with agent.session(provider_history={"type": "previous_response"}) as original:
            await original.submit("Start.")
            await next_request(original.interactions(), lookup)
            state = await original.suspend()
        row = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": STAGED}],
        }
        state = state.with_pending_input([row, row])
        async with agent.session(
            state=state, provider_history={"type": "previous_response", "id": "resp_1"}
        ) as resumed:
            first = await next_request(resumed.interactions(), lookup)
            await first.respond("accepted")
            assert resumed.restored_turn is not None
            with pytest.raises(NativeError):
                await resumed.restored_turn.get_result()
            saved = await resumed.save_state()
            assert saved.pending_input == [row, row]
            assert saved.native_continuation is not None and saved.native_continuation.staged_retry
            assert not staged_occurrences(saved.history)
        assert len(model.requests) == 2
        assert model.requests[-1]["previous_response_id"] == "resp_1"
        assert len(staged_occurrences(model.requests[-1]["input"])) == 1
        assert wire.count(("POST", "/v1/responses")) == 2


@pytest.mark.asyncio
async def test_terminal_accepts_selected_rows_before_failing_observer():
    lookup = FunctionTool(
        name="lookup", description="Read a record.", input_type=LookupArgs, output_type=str
    )

    class ObserverError(ValueError):
        pass

    selections = []

    def transform(request):
        matches = staged_occurrences(request.items)
        if len(matches) != 2 or selections:
            return None
        assert matches[0] is not matches[1]
        selections.append({"omitted": matches[0], "selected": matches[1]})
        return [item for item in request.items if item is not matches[0]]

    observed_responses = []

    def after_model(response):
        observed_responses.append(response.response_id)
        if response.response_id == "resp_2":
            raise ObserverError("synthetic observer refuses completion")

    async with timeout(25), history_peer() as (provider, model, _, _url, wire):
        model.respond(function_call("lookup", {"key": "first"}, "manual-one"))
        model.respond(function_call("lookup", {"key": "blocked"}, "manual-blocked"))
        agent = Agent(
            instructions="Read twice.",
            model="gpt-5.5",
            provider=provider,
            tools=[lookup],
            model_settings={"store": True},
            transform_context=transform,
            after_model=after_model,
        )
        async with agent.session(provider_history={"type": "previous_response"}) as original:
            await original.submit("Start.")
            await next_request(original.interactions(), lookup)
            state = await original.suspend()
        row = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": STAGED}],
        }
        state = state.with_pending_input([row, row])
        async with agent.session(
            state=state, provider_history={"type": "previous_response", "id": "resp_1"}
        ) as resumed:
            first = await next_request(resumed.interactions(), lookup)
            await first.respond("accepted")
            assert resumed.restored_turn is not None
            with pytest.raises(ObserverError, match="synthetic observer refuses completion"):
                await resumed.restored_turn.get_result()
            saved = await resumed.save_state()
            assert len(selections) == 1
            assert saved.pending_input == [row]
            assert len(staged_occurrences(saved.history)) == 1
            assert '"resp_2"' in json.dumps(saved.history)
        assert len(model.requests) == 2
        model.respond(assistant_output("cold passive complete"))
        cold_state = SessionState.from_json(saved.to_json())
        assert cold_state.pending_input == [row]
        async with agent.session(
            state=cold_state,
            provider_history={"type": "previous_response", "id": "resp_2"},
        ) as cold:
            turn = cold.restored_turn or await cold.submit("")
            result = await turn.get_result()
            assert result.final_output == "cold passive complete"
            drained = await cold.save_state()
            assert drained.pending_input == []
            assert len(staged_occurrences(drained.history)) == 2
        assert observed_responses == ["resp_1", "resp_2", "resp_3"]
        assert len(model.requests) == 3
        assert model.requests[-1]["previous_response_id"] == "resp_2"
        assert len(staged_occurrences(model.requests[-1]["input"])) == 1
        blocked = [
            item for item in model.requests[-1]["input"] if item.get("call_id") == "manual-blocked"
        ]
        assert [(item["type"], item["output"]) for item in blocked] == [
            ("function_call_output", "aborted")
        ]
        assert not any(item.get("call_id") == "manual-one" for item in model.requests[-1]["input"])
        assert len(selections) == 1
        assert (
            sum(
                item.get("type") == "function_call_output" and item.get("call_id") == "manual-one"
                for request in model.requests
                for item in request["input"]
            )
            == 1
        )
        assert model.requests[1]["previous_response_id"] == "resp_1"
        assert wire.count(("POST", "/v1/responses")) == 3
