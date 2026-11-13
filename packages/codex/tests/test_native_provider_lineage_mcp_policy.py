import json

import pytest

from agents import Agent, Allow, HttpMCP, ReplaceArguments
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output
from tests.test_native_mcp_tools import Peer, mcp_call
from tests.test_native_provider_lineage import history_peer


@pytest.mark.parametrize("decision", ["changed", "allow", "equal", "equal_formatting"])
@pytest.mark.parametrize("cold", [False, True], ids=["live", "cold"])
@pytest.mark.parametrize("sampling", [False, True], ids=["original_input", "sampling_input"])
async def test_mcp_argument_policy_keeps_provider_history_usable(decision, cold, sampling):
    peer = Peer(names=("search",))
    before_calls = []

    def before(_context, _call):
        before_calls.append(True)
        if decision == "allow":
            return Allow()
        return ReplaceArguments({"key": "effective" if decision == "changed" else "original"})

    def transform(request):
        result = [dict(item) for item in request.items]
        index = next(index for index, item in enumerate(result) if item.get("role") == "user")
        result[index]["content"] = [{"type": "input_text", "text": "sampling-user"}]
        return result

    async with timeout(30), history_peer() as (provider, model, _reads, _base, _wire):
        call = mcp_call("search", "mcp__records", "rewritten")
        call["item"]["arguments"] = (
            ' { "key" : "original" } ' if decision == "equal_formatting" else '{"key":"original"}'
        )
        model.respond(call)
        model.respond(assistant_output("Settled."))
        model.respond(assistant_output("Still usable."))
        async with peer.serve() as url:
            agent = Agent(
                instructions="Search.",
                model="gpt-5.5",
                provider=provider,
                model_settings={"store": True},
                transform_context=transform if sampling else None,
                tools=[
                    HttpMCP(
                        name="records",
                        url=url,
                        tools=["search"],
                        approval_mode="preapproved",
                        before=before,
                    )
                ],
            )
            async with agent.session(provider_history={"type": "previous_response"}) as session:
                await (await session.submit("original-user")).get_result()
                assert before_calls == [True]
                assert len(peer.calls) == 1
                assert peer.calls[0]["arguments"] == {
                    "key": "effective" if decision == "changed" else "original"
                }
                assert len(model.requests) == 2
                assert all(
                    item.get("type") != "function_call" for item in model.requests[1]["input"]
                )
                history = await session.history_snapshot()
                local = next(item for item in history.items if item.get("type") == "function_call")
                assert json.loads(local["arguments"]) == peer.calls[0]["arguments"]
                assert "original-user" in json.dumps(history.items)
                assert "sampling-user" not in json.dumps(history.items)
                if cold:
                    state = await session.save_state()
                    await session.aclose()
                    async with agent.session(
                        state=state, provider_history={"type": "previous_response", "id": "resp_2"}
                    ) as restored:
                        await (await restored.submit("Continue.")).get_result()
                else:
                    await (await session.submit("Continue.")).get_result()
                assert len(model.requests) == 3
                assert len(peer.calls) == 1
