"""Private source proof of the existing entered-call continuation owner."""

import json
from dataclasses import replace

import pytest

from agents import ApprovalRequested, InteractionExpiredError, PendingRequests, SessionState
from tests._asyncio import timeout
from tests.test_native_configured_agent_tool import fixture


async def test_entered_configured_call_parks_once_after_owned_child_seal():
    async with timeout(35), fixture() as values:
        parent, child_agent, context, root_model, child_model, effects, _guards, sibling = values
        async with parent.session(context=context) as session:
            turn = await session.submit("begin joint witness")
            boundary = await turn.wait_for_request_or_result()
            assert isinstance(boundary, PendingRequests)
            approval = boundary.requests[0]
            assert isinstance(approval, ApprovalRequested)
            await sibling.wait()
            [child] = session._configured_children
            child.sealing = True
            saved_child = await child.session.suspend()
            assert saved_child.native_continuation is not None
            assert saved_child.native_continuation.pending_manual[0].call_id == approval.call_id
            payload = {
                "request": json.loads(child.parent_request),
                "callback": child.parent_callback,
                "continuation": {
                    "kind": "configured-child-v1",
                    "state": json.loads(saved_child.to_json()),
                },
            }
            assert child.parent_callback is not None
            stale = {
                **payload,
                "callback": {
                    **child.parent_callback,
                    "invocation": str(int(child.parent_callback["invocation"]) + 1),
                },
            }
            with pytest.raises(RuntimeError, match="live entered call"):
                await session._native._respond_python_tool(json.dumps(stale))
            assert await session._native._respond_python_tool(json.dumps(payload)) is True
            with pytest.raises(RuntimeError, match="live entered call"):
                await session._native._respond_python_tool(json.dumps(payload))
            suspended = json.loads(await session._native._suspend_python_tools())
            assert suspended["turn_id"] == turn.turn_id
            assert effects == ["accepted-sibling"]
            assert len(root_model.requests) == len(child_model.requests) == 1

            portable = SessionState.from_json(saved_child.to_json())
            with pytest.raises((ValueError, RuntimeError), match="tool|decl|profile"):
                async with replace(child_agent, tools=[]).session(context=context, state=portable):
                    raise AssertionError("missing current child tool declaration was admitted")
            assert effects == ["accepted-sibling"]
            assert len(root_model.requests) == len(child_model.requests) == 1
            async with child_agent.session(context=context, state=portable) as restored:
                assert restored.restored_turn is not None
                resumed = await restored.restored_turn.wait_for_request_or_result()
                assert isinstance(resumed, PendingRequests)
                [current] = resumed.requests
                assert isinstance(current, ApprovalRequested)
                assert current.call_id == approval.call_id
                assert current.session_id == approval.session_id
                assert current.submission_id != approval.submission_id
                with pytest.raises(InteractionExpiredError):
                    await restored.respond(approval.reply(True))
                reply = current.reply(True)
                await restored.respond(reply)
                with pytest.raises(InteractionExpiredError):
                    await restored.respond(reply)
                assert (await restored.restored_turn.get_result()).final_output == "child complete"
            assert effects == ["accepted-sibling", "child-effect"]
            assert len(root_model.requests) == 1
            assert len(child_model.requests) == 2


@pytest.mark.parametrize("legacy_usage", [False, True])
async def test_public_parent_checkpoint_resumes_configured_child_and_held_sibling_once(
    legacy_usage,
):
    async with timeout(35), fixture() as values:
        parent, _child, context, root_model, child_model, effects, guards, sibling = values
        async with parent.session(context=context) as source:
            turn = await source.submit("begin portable parent")
            boundary = await turn.wait_for_request_or_result()
            assert isinstance(boundary, PendingRequests)
            [original] = boundary.requests
            assert isinstance(original, ApprovalRequested)
            await sibling.wait()
            saved = await source.suspend()
            assert (turn.run_usage.requests, turn.run_usage.total_tokens) == (2, 28)
        if not legacy_usage:
            value = json.loads(saved.to_json())
            value["operation_tools"] = []
            excluded = SessionState.from_json(json.dumps(value))
            with pytest.raises(ValueError, match="Saved operation does not admit"):
                async with parent.session(context=context, state=excluded):
                    raise AssertionError("excluded configured child was admitted")
        if legacy_usage:
            value = json.loads(saved.to_json())
            [continuation] = [
                entry["continuation"]
                for entry in value["native_continuation"]["pending_manual"]
                if entry.get("continuation")
            ]
            child_usage = continuation["state"]["run_usage"]
            continuation.pop("usage_in_parent")
            value["run_usage"] = {
                key: count - child_usage[key] for key, count in value["run_usage"].items()
            }
            saved = SessionState.from_json(json.dumps(value))
        with pytest.raises((ValueError, RuntimeError), match="tool|decl|operation"):
            async with replace(parent, tools=[]).session(context=context, state=saved):
                raise AssertionError("missing current configured declaration was admitted")
        assert effects == ["accepted-sibling"]
        assert len(root_model.requests) == len(child_model.requests) == 1
        async with parent.session(
            context=context, state=SessionState.from_json(saved.to_json())
        ) as restored:
            assert restored.restored_turn is not None
            assert (
                restored.restored_turn.run_usage.requests,
                restored.restored_turn.run_usage.total_tokens,
            ) == (2, 28)
            pending = await restored.restored_turn.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            [current] = pending.requests
            assert isinstance(current, ApprovalRequested)
            assert current.call_id == original.call_id
            assert effects == ["accepted-sibling"]
            with pytest.raises(InteractionExpiredError):
                await restored.respond(original.reply(True))
            await restored.respond(current.reply(True))
            with pytest.raises(InteractionExpiredError):
                await restored.respond(current.reply(True))
            completed = await restored.restored_turn.get_result()
            assert completed.final_output == "parent complete"
            assert (completed.run_usage.requests, completed.run_usage.total_tokens) == (4, 56)
            assert restored.restored_turn.run_usage == completed.run_usage
        assert effects == ["accepted-sibling", "child-effect"]
        assert len(root_model.requests) == len(child_model.requests) == 2
        assert [name for name, _agent in guards] == [
            "parent-input-owner",
            "child-input-owner",
            "child-output-owner",
            "parent-output-owner",
        ]


async def test_parent_preseal_refusal_rolls_back_owned_child_without_repeating_sibling():
    async with timeout(35), fixture() as values:
        parent, _child, context, root_model, child_model, effects, guards, sibling = values
        async with parent.session(context=context) as source:
            turn = await source.submit("begin rollback parent")
            original = await turn.wait_for_request_or_result()
            assert isinstance(original, PendingRequests)
            original_request = original.requests[0]
            assert isinstance(original_request, ApprovalRequested)
            await sibling.wait()

            def refuse():
                raise RuntimeError("injected parent refusal")

            with pytest.raises(RuntimeError, match="injected parent refusal"):
                await source._suspend(before_seal=refuse)
            [child] = source._configured_children
            assert child.session._sealed_checkpoint is None
            assert (turn.run_usage.requests, turn.run_usage.total_tokens) == (2, 28)
            current = await turn.wait_for_request_or_result()
            assert isinstance(current, PendingRequests)
            [request] = current.requests
            assert isinstance(request, ApprovalRequested)
            assert request.call_id == original_request.call_id
            assert effects == ["accepted-sibling"]
            with pytest.raises(InteractionExpiredError):
                await source.respond(original_request.reply(True))
            await source.respond(request.reply(True))
            with pytest.raises(InteractionExpiredError):
                await source.respond(request.reply(True))
            completed = await turn.get_result()
            assert completed.final_output == "parent complete"
            assert (completed.run_usage.requests, completed.run_usage.total_tokens) == (4, 56)
        assert effects == ["accepted-sibling", "child-effect"]
        assert len(root_model.requests) == len(child_model.requests) == 2
        assert [name for name, _agent in guards] == [
            "parent-input-owner",
            "child-input-owner",
            "child-output-owner",
            "parent-output-owner",
        ]


async def test_configured_child_denial_keeps_admitted_usage():
    async with timeout(35), fixture() as values:
        parent, _child, context, root_model, child_model, effects, _guards, sibling = values
        async with parent.session(context=context) as source:
            turn = await source.submit("deny configured leaf")
            pending = await turn.wait_for_request_or_result()
            await sibling.wait()
            assert isinstance(pending, PendingRequests)
            [request] = pending.requests
            assert isinstance(request, ApprovalRequested)
            await source.respond(request.reply(False))
            completed = await turn.get_result()
            assert completed.final_output == "parent complete"
            assert (completed.run_usage.requests, completed.run_usage.total_tokens) == (4, 56)
            assert effects == ["accepted-sibling"]
            assert len(root_model.requests) == len(child_model.requests) == 2
