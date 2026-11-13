import json
from dataclasses import replace

import pytest

from agents import (
    ApprovalRequested,
    GuardrailResult,
    GuardrailTripwire,
    OutputGuardrail,
    PendingRequests,
    SessionState,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, text_response
from tests.test_native_configured_agent_tool import fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("trailing_reasoning", [False, True])
async def test_parent_guardrail_withholds_after_restored_child_approval(trailing_reasoning):
    async with timeout(35), fixture() as values:
        parent, _child, context, root_model, child_model, effects, guards, sibling = values
        guard_seen = []
        rejected = "parent complete"
        root_model.replies.pop()
        events = text_response("msg_guarded_parent", "parent ", "complete")
        if trailing_reasoning:
            events.append(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "reasoning",
                        "id": "rs_after_guarded_parent",
                        "summary": [{"type": "summary_text", "text": "trailing reasoning"}],
                    },
                }
            )
        root_model.respond(*events)
        earlier = "earlier approved output"
        root_model.replies.appendleft([assistant_output(earlier)])

        def reject(request, _agent):
            guard_seen.append(request.final_output)
            return GuardrailResult(request.final_output == rejected)

        parent = replace(parent, output_guardrails=[OutputGuardrail("reject-output", reject)])
        async with parent.session(context=context) as session:
            earlier_turn = await session.submit("record an earlier answer")
            assert (await earlier_turn.get_result()).final_output == earlier
            turn = await session.submit("begin portable parent")
            boundary = await turn.wait_for_request_or_result()
            assert isinstance(boundary, PendingRequests)
            await sibling.wait()
            snapshot = await session.suspend()
        async with parent.session(
            context=context, state=SessionState.from_json(snapshot.to_json())
        ) as session:
            turn = session.restored_turn
            assert turn is not None
            boundary = await turn.wait_for_request_or_result()
            assert isinstance(boundary, PendingRequests)
            [approval] = boundary.requests
            assert isinstance(approval, ApprovalRequested)
            await session.respond(approval.reply(True))
            with pytest.raises(GuardrailTripwire) as error:
                await turn.get_result()
            assert error.value.record.name == "reject-output"
            assert guard_seen == [earlier, rejected]
            assert effects == ["accepted-sibling", "child-effect"]
            assert len(root_model.requests) == 3
            assert len(child_model.requests) == 2
            public_history = [item async for item in session.items()]
            checkpoint = await session.save_state()
            assert earlier in json.dumps(public_history)
            assert earlier in checkpoint.to_json()
            assert rejected not in repr(
                [event async for event in turn.events(include_execution_details=True)]
            )
            public_leak = rejected in json.dumps(public_history)
            checkpoint_leak = rejected in checkpoint.to_json()
            root_model.respond(assistant_output("continued"))
            successor = await session.submit("continue after refusal")
            assert (await successor.get_result()).final_output == "continued"
            assert effects == ["accepted-sibling", "child-effect"]
            provider_leak = rejected in json.dumps(root_model.requests[-1])
            assert not any((public_leak, checkpoint_leak, provider_leak)), (
                public_leak,
                checkpoint_leak,
                provider_leak,
            )
