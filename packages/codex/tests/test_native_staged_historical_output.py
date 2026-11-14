import pytest

from agents import Agent, FunctionTool, NativeError, SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_provider_lineage import history_peer


@pytest.mark.parametrize("kind", ["previous_response", "conversation"])
@pytest.mark.parametrize("item_type", ["function_call_output", "local_shell_call_output"])
async def test_detached_staged_output_is_history_only(kind, item_type):
    manual = FunctionTool(
        name="lookup", description="Read a record.", input_type=LookupArgs, output_type=str
    )
    historical = {
        "type": item_type,
        ("call_id" if item_type == "function_call_output" else "id"): "historical-call",
        "output": "historical result",
    }
    selection = {"type": kind}
    if kind == "conversation":
        selection["id"] = "conv_current"
    async with timeout(15), history_peer() as (provider, model, _, _, _):
        model.respond(function_call("lookup", {"key": "record"}, "actual-call"))
        model.respond(assistant_output("continued"))
        model.respond(assistant_output("later"))
        agent = Agent(
            instructions="Read a record.",
            model="gpt-5.5",
            provider=provider,
            tools=[manual],
            model_settings={"store": True},
        )
        async with agent.session(provider_history=selection) as source:
            await source.submit("Initial input")
            await next_request(source.interactions(), manual)
            saved = await source.suspend()
        state = SessionState.from_json(saved.with_pending_input([historical]).to_json())
        live_selection = {
            "type": kind,
            "id": "resp_1" if kind == "previous_response" else "conv_current",
        }
        async with agent.session(state=state, provider_history=live_selection) as reopened:
            first = await next_request(reopened.interactions(), manual)
            assert first.call_id == "actual-call"
            await first.respond("actual result")
            assert reopened.restored_turn is not None
            if item_type == "function_call_output":
                with pytest.raises(NativeError, match="Unsupported item in terminal staged input"):
                    await reopened.restored_turn.get_result()
                assert len(model.requests) == 1
                return
            assert (await reopened.restored_turn.get_result()).final_output == "continued"
            assert (await (await reopened.submit("later")).get_result()).final_output == "later"
            state = await reopened.save_state()
            assert not state.pending_input

    assert len(model.requests) == 3
    matching = [
        row for row in model.requests[1]["input"] if row.get("output") == "historical result"
    ]
    assert matching == [historical]
    assert sum(row.get("call_id") == "actual-call" for row in model.requests[1]["input"]) == 1
    assert not any(row.get("output") == "historical result" for row in model.requests[2]["input"])
