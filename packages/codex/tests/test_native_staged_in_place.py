import pytest

from agents import Agent, FunctionTool, NativeError, SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_provider_lineage import history_peer


@pytest.mark.parametrize("kind", ["previous_response", "conversation"])
@pytest.mark.parametrize("same_object", [True, False])
async def test_filter_rewrites_same_staged_occurrence(kind, same_object):
    manual = FunctionTool(
        name="lookup", description="Read a record.", input_type=LookupArgs, output_type=str
    )
    original, projected = "original pending text", "filtered pending text"
    transformations = []

    def transform(request):
        if transformations:
            return None
        for row in request.items:
            if row.get("role") == "user" and row["content"][0].get("text") == original:
                transformations.append(row)
                if same_object:
                    row["content"][0]["text"] = projected
                    return request.items
                return [
                    {**item, "content": [{"type": "input_text", "text": projected}]}
                    if item is row
                    else item
                    for item in request.items
                ]
        return None

    def count(items, marker):
        return sum(
            part.get("text") == marker
            for item in items
            if item.get("role") == "user"
            for part in item.get("content", ())
        )

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
            transform_context=transform,
        )
        async with agent.session(provider_history=selection) as source:
            await source.submit("Initial input")
            await next_request(source.interactions(), manual)
            saved = await source.suspend()
        saved = SessionState.from_json(saved.with_pending_input(original).to_json())
        restored_selection = {
            "type": kind,
            "id": "resp_1" if kind == "previous_response" else "conv_current",
        }
        async with agent.session(state=saved, provider_history=restored_selection) as reopened:
            pending = await next_request(reopened.interactions(), manual)
            await pending.respond("actual result")
            assert reopened.restored_turn is not None
            if not same_object:
                with pytest.raises(NativeError, match="original user objects"):
                    await reopened.restored_turn.get_result()
                assert len(model.requests) == 1
                assert (await reopened.save_state()).pending_input == saved.pending_input
                return
            assert (await reopened.restored_turn.get_result()).final_output == "continued"
            saved = await reopened.save_state()
            assert not saved.pending_input
            assert (
                count(
                    (row["payload"] for row in saved.history if row.get("type") == "response_item"),
                    original,
                )
                == 1
            )

        next_selection = {
            "type": kind,
            "id": "resp_2" if kind == "previous_response" else "conv_current",
        }
        async with agent.session(
            state=SessionState.from_json(saved.to_json()), provider_history=next_selection
        ) as final:
            assert (await (await final.submit("later")).get_result()).final_output == "later"

    assert len(transformations) == 1
    assert len(model.requests) == 3
    assert count(model.requests[1]["input"], projected) == 1
    assert not count(model.requests[1]["input"], original)
    assert not count(model.requests[2]["input"], projected)
    assert not count(model.requests[2]["input"], original)
