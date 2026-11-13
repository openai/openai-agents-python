"""A restored parent has been retaining its child's latest approval boundary."""

import asyncio

import pytest

from agents import (
    Agent,
    ApprovalRequested,
    InteractionExpiredError,
    NativeProvider,
    PendingRequests,
    SessionState,
    ToolContext,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_configured_agent_tool import MODEL, peer


async def test_restored_parent_reseal_retains_second_child_approval_without_repeating_effects():
    async with timeout(45), peer() as root_model, peer() as child_model:
        effects = []
        sibling_done = asyncio.Event()

        @function_tool(needs_approval=True)
        def child_effect(context: ToolContext[None]) -> str:
            effects.append(context.call.call_id)
            return "child effect accepted"

        @function_tool
        def sibling() -> str:
            effects.append("sibling")
            sibling_done.set()
            return "accepted sibling"

        root_model.respond(
            function_call("nested_child", {"input": "request two approvals"}, "parent-call"),
            function_call("sibling", {}, "sibling-call"),
        )
        root_model.respond(assistant_output("parent complete"))
        child_model.respond(function_call("child_effect", {}, "child-first"))
        child_model.respond(function_call("child_effect", {}, "child-second"))
        child_model.respond(assistant_output("child complete"))
        async with (
            NativeProvider(
                base_url=root_model.url, api_key="synthetic-reseal-root"
            ) as root_provider,
            NativeProvider(
                base_url=child_model.url, api_key="synthetic-reseal-child"
            ) as child_provider,
        ):
            child = Agent(
                name="Child",
                instructions="Request two effects.",
                model=MODEL,
                provider=child_provider,
                tools=[child_effect],
            )
            parent = Agent(
                instructions="Run child and sibling.",
                model=MODEL,
                provider=root_provider,
                tools=[child.as_tool("nested_child"), sibling],
            )
            async with parent.session() as source:
                turn = await source.submit("begin")
                first = await turn.wait_for_request_or_result()
                assert isinstance(first, PendingRequests)
                [original] = first.requests
                assert isinstance(original, ApprovalRequested)
                assert original.call_id == "child-first"
                await sibling_done.wait()
                saved_first = await source.suspend()
            async with parent.session(
                state=SessionState.from_json(saved_first.to_json())
            ) as resumed:
                turn = resumed.restored_turn
                assert turn is not None
                first = await turn.wait_for_request_or_result()
                assert isinstance(first, PendingRequests)
                [reentered] = first.requests
                assert isinstance(reentered, ApprovalRequested)
                await resumed.respond(reentered.reply(True))
                second = await turn.wait_for_request_or_result()
                assert isinstance(second, PendingRequests)
                [next_approval] = second.requests
                assert isinstance(next_approval, ApprovalRequested)
                assert next_approval.call_id == "child-second"
                assert effects == ["sibling", "child-first"]
                saved_second = await resumed.suspend()
                prefix_usage = turn.run_usage
            async with parent.session(
                state=SessionState.from_json(saved_second.to_json())
            ) as final:
                turn = final.restored_turn
                assert turn is not None
                assert turn.run_usage == prefix_usage
                pending = await turn.wait_for_request_or_result()
                assert isinstance(pending, PendingRequests)
                [current] = pending.requests
                assert isinstance(current, ApprovalRequested)
                assert current.call_id == "child-second"
                with pytest.raises(InteractionExpiredError):
                    await final.respond(original.reply(True))
                await final.respond(current.reply(True))
                result = await turn.get_result()
                assert result.final_output == "parent complete"
                assert result.run_usage.requests == 5
            assert effects == ["sibling", "child-first", "child-second"]
            assert len(root_model.requests) == 2
            assert len(child_model.requests) == 3
