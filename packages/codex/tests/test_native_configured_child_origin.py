import dataclasses

import pytest

from agents import Agent, NativeProvider
from agents.models import ModelResponse


@pytest.mark.asyncio
async def test_configured_child_items_keep_the_admitted_origin():
    root_requests = 0
    child_requests = 0
    child_result = None

    def usage(input_tokens, output_tokens):
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

    def message(text):
        return {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }

    class ChildModel:
        def complete(self, request):
            nonlocal child_requests
            child_requests += 1
            assert request.body["model"] == "fixture-child"
            return ModelResponse([message("child done")], usage(7, 2), response_id="child-response")

    class RootModel:
        def complete(self, request):
            nonlocal root_requests
            root_requests += 1
            assert request.body["model"] == "fixture-root"
            if root_requests == 1:
                return ModelResponse(
                    [
                        {
                            "type": "function_call",
                            "id": "delegate-item",
                            "call_id": "delegate-call",
                            "name": "delegate",
                            "arguments": '{"input":"work"}',
                        }
                    ],
                    usage(3, 1),
                    response_id="parent-first",
                )
            return ModelResponse([message("parent done")], usage(5, 1), response_id="parent-last")

    def extract(result):
        nonlocal child_result
        child_result = result
        return result.final_output

    async with NativeProvider(
        base_url="https://configured-child.invalid/v1",
        api_key="synthetic-g14",
        supports_websockets=False,
        model_backend=ChildModel(),
    ) as child_provider:
        child = Agent(
            name="Worker",
            instructions="Finish the work.",
            model="fixture-child",
            provider=child_provider,
        )
        async with NativeProvider(
            base_url="https://configured-parent.invalid/v1",
            api_key="synthetic-g14",
            supports_websockets=False,
            model_backend=RootModel(),
        ) as root_provider:
            root = Agent(
                name="Parent",
                instructions="Delegate then finish.",
                model="fixture-root",
                provider=root_provider,
                tools=[child.as_tool("delegate", custom_output_extractor=extract)],
            )
            result = await root.run("Start.", return_items=True)
    entries = [
        entry for entry in result.items or () if entry.item.get("call_id") == "delegate-call"
    ]
    assert child_result is not None
    observation = {
        "root_requests": root_requests,
        "child_requests": child_requests,
        "root_usage": dataclasses.asdict(result.run_usage),
        "child_usage": dataclasses.asdict(child_result.run_usage),
        "final_output": result.final_output,
        "item_types": [entry.item["type"] for entry in entries],
        "origins": [
            dataclasses.asdict(entry.tool_origin) if entry.tool_origin else None
            for entry in entries
        ],
        "owners": [entry.agent.name for entry in entries],
    }
    assert (child_result.run_usage.requests, child_result.run_usage.total_tokens) == (1, 9)
    assert root_requests == 2 and child_requests == 1
    assert result.final_output == "parent done"
    assert observation["item_types"] == ["function_call", "function_call_output"]
    failures = []
    if result.run_usage.requests != 3 or result.run_usage.total_tokens != 19:
        failures.append(f"parent usage excludes configured child: {observation['root_usage']}")
    assert not failures, "; ".join(failures)

    assert all(
        origin is not None and origin["name"] == "delegate" and origin["role"] == "Worker"
        for origin in observation["origins"]
    ), observation["origins"]


async def test_reopened_configured_child_attributes_the_new_result() -> None:
    from agents import ApprovalRequested, PendingRequests
    from tests._asyncio import timeout
    from tests.test_native_configured_agent_tool import fixture

    async with timeout(20), fixture() as values:
        parent, _child, context, _root_model, _child_model, effects, _guards, sibling = values
        async with parent.session(context=context) as source:
            turn = await source.submit("Capture the child before approval.")
            assert isinstance(await turn.wait_for_request_or_result(), PendingRequests)
            await sibling.wait()
            saved = await source.suspend()
        async with parent.session(context=context, state=saved) as restored:
            turn = restored.restored_turn
            assert turn is not None
            restored._start_produced_items()
            pending = await turn.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            (request,) = pending.requests
            assert isinstance(request, ApprovalRequested)
            await restored.respond(request.reply(True))
            assert (await turn.get_result()).final_output == "parent complete"
            (entry,) = [
                produced
                for produced in restored._take_produced_items(turn)
                if produced.item.get("call_id") == "nested-parent-call"
            ]
            assert entry.item["type"] == "function_call_output"
            assert entry.tool_origin is not None
            assert (entry.tool_origin.name, entry.tool_origin.role) == (
                "nested_child",
                "Nested guarded child",
            )
            assert effects == ["accepted-sibling", "child-effect"]
