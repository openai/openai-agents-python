"""Historical local-shell output is data, with its provider correlation unchanged."""

import pytest

from agents import Agent, FunctionTool, NativeProvider, SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_manual_tools import LookupArgs, next_request


@pytest.mark.parametrize("reuse_current_call", [False, True, "misleading-call-id"])
async def test_pending_local_shell_output_keeps_historical_identity(responses, reuse_current_call):
    lookup = FunctionTool(
        name="lookup", description="Read.", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "record"}, "current-manual"))
    if not reuse_current_call:
        responses.respond(assistant_output("continued"))
    async with (
        timeout(20),
        NativeProvider(
            base_url=responses.url, api_key="synthetic-legacy-pending", supports_websockets=False
        ) as provider,
    ):
        agent = Agent(instructions="Continue.", provider=provider, tools=[lookup])
        async with agent.session() as source:
            await source.submit("initial")
            await next_request(source.interactions(), lookup)
            saved = await source.suspend()
        historical = {
            "type": "local_shell_call_output",
            "id": "current-manual" if reuse_current_call else "historical-shell",
            "output": "historical result",
        }
        if reuse_current_call == "misleading-call-id":
            historical["call_id"] = "unrelated-ignored-field"
        staged = SessionState.from_json(saved.with_pending_input([historical]).to_json())
        assert staged.pending_input == [historical]
        if reuse_current_call:
            with pytest.raises(RuntimeError, match="Staged input cannot reuse a call"):
                async with agent.session(state=staged):
                    pass
            assert len(responses.requests) == 1
            return
        async with agent.session(state=staged) as restored:
            current = await next_request(restored.interactions(), lookup)
            await current.respond("current accepted result")
            assert (await restored.restored_turn.get_result()).final_output == "continued"
    assert len(responses.requests) == 2
    wire = responses.requests[1]["input"]
    assert historical in wire
    assert sum(item.get("type") == "function_call_output" for item in wire) == 1
    assert not any(item.get("type") == "local_shell_call" for item in wire)
    current_index = next(
        i
        for i, item in enumerate(wire)
        if item.get("call_id") == "current-manual" and item.get("type") == "function_call_output"
    )
    assert wire.index(historical) > current_index
