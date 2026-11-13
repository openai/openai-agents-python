"""One-shot children have been retaining their independent live-reply requirement."""

from dataclasses import replace

import pytest
from pydantic import BaseModel

from agents import ApprovalRequested, FunctionTool, InteractionRequired, PendingRequests
from tests._asyncio import timeout
from tests.test_native_configured_agent_tool import fixture


class EmptyInput(BaseModel):
    pass


async def test_one_shot_child_refuses_missing_handler_with_unused_manual_tool():
    async with timeout(20), fixture() as values:
        parent, _child, context, root_model, child_model, effects, _guards, _sibling = values
        unused = FunctionTool(
            name="unused_manual",
            description="Uncalled root manual tool.",
            input_type=EmptyInput,
            output_type=str,
        )
        parent = replace(parent, tools=(*parent.tools, unused))
        with pytest.raises(InteractionRequired, match="Configured child"):
            await parent.run("begin joint witness", context=context)
        assert effects == ["accepted-sibling"]
        assert len(root_model.requests) == len(child_model.requests) == 1


async def test_live_child_keeps_root_reply_owner_with_unused_manual_tool():
    async with timeout(20), fixture() as values:
        parent, _child, context, root_model, child_model, effects, _guards, _sibling = values
        unused = FunctionTool(
            name="unused_manual",
            description="Uncalled root manual tool.",
            input_type=EmptyInput,
            output_type=str,
        )
        parent = replace(parent, tools=(*parent.tools, unused))
        async with parent.session(context=context) as session:
            turn = await session.submit("begin joint witness")
            boundary = await turn.wait_for_request_or_result()
            assert isinstance(boundary, PendingRequests)
            request = next(x for x in boundary.requests if isinstance(x, ApprovalRequested))
            assert request.call_id == "child-approval-call"
            await session.respond(request.reply(True))
            assert (await turn.get_result()).final_output == "parent complete"
        assert effects == ["accepted-sibling", "child-effect"]
        assert len(root_model.requests) == len(child_model.requests) == 2
