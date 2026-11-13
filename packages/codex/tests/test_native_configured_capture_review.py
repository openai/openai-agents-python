"""Configured capture ownership across passive cancellation and live rollback."""

import asyncio
import json
from dataclasses import replace

import pytest

from agents import Agent, ApprovalRequested, CapabilityError, Environment, PendingRequests
from tests._asyncio import timeout
from tests.test_native_configured_agent_tool import fixture


@pytest.mark.parametrize("waiter", ["cancel", "duplicate", "close"])
async def test_configured_capture_joins_child_seal_before_waiter_or_close(monkeypatch, waiter):
    async with timeout(35), fixture() as values:
        parent, _child, context, root_model, child_model, effects, _guards, sibling = values
        async with parent.session(context=context) as source:
            turn = await source.submit("Capture the child once.")
            boundary = await turn.wait_for_request_or_result()
            assert isinstance(boundary, PendingRequests)
            await sibling.wait()
            [child] = source._configured_children
            suspend_child = child.session.suspend
            sealed = asyncio.Event()
            release = asyncio.Event()
            calls = 0

            async def hold_after_native_seal():
                nonlocal calls
                calls += 1
                saved = await suspend_child()
                sealed.set()
                await release.wait()
                return saved

            monkeypatch.setattr(child.session, "suspend", hold_after_native_seal)
            first = asyncio.create_task(source.suspend())
            others = []
            try:
                await sealed.wait()
                if waiter == "cancel":
                    first.cancel()
                elif waiter == "duplicate":
                    others.append(asyncio.create_task(source.suspend()))
                else:
                    others.append(asyncio.create_task(source.aclose()))
                done, _ = await asyncio.wait([first, *others], timeout=0.05)
                assert not done
            finally:
                release.set()
            if waiter == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await first
                saved = await source.suspend()
            else:
                saved = await first
                for other in others:
                    outcome = await other
                    if waiter == "duplicate":
                        assert outcome.to_json() == saved.to_json()
            assert calls == 1
        async with parent.session(context=context, state=saved) as restored:
            assert restored.restored_turn is not None
            pending = await restored.restored_turn.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            [request] = pending.requests
            assert isinstance(request, ApprovalRequested)
            await restored.respond(request.reply(True))
            assert (await restored.restored_turn.get_result()).final_output == "parent complete"
        assert effects == ["accepted-sibling", "child-effect"]
        assert len(root_model.requests) == len(child_model.requests) == 2


async def test_configured_capture_rejects_handoff_profiles_before_retiring_child():
    async with timeout(35), fixture() as values:
        parent, _child, context, _root_model, _child_model, effects, _guards, sibling = values
        target = Agent(name="Unused handoff target", instructions="Never invoked.")
        parent = replace(parent, handoffs=[target])
        async with parent.session(context=context) as source:
            turn = await source.submit("Keep the live approval.")
            boundary = await turn.wait_for_request_or_result()
            assert isinstance(boundary, PendingRequests)
            [request] = boundary.requests
            assert isinstance(request, ApprovalRequested)
            await sibling.wait()
            [child] = source._configured_children
            with pytest.raises(CapabilityError, match="handoff profiles"):
                await source.suspend()
            assert source._sealed_checkpoint is None
            assert child.session._sealed_checkpoint is None
            await source.respond(request.reply(True))
            assert (await turn.get_result()).final_output == "parent complete"
        assert effects == ["accepted-sibling", "child-effect"]


async def test_configured_rollback_keeps_issuing_executor_tool_ceiling(
    tmp_path, monkeypatch, native_executor_binary
):
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import trust

    async with timeout(45), fixture() as values:
        parent, _child, context, _root_model, child_model, effects, _guards, sibling = values
        async with ExecutorPeer(tmp_path / "executor").serve() as executor:
            trust(executor, tmp_path, monkeypatch)
            environment = Environment(
                url=executor.url,
                headers={"Authorization": "Bearer " + executor.token},
                cwd=str(executor.root),
                tools=["exec_command"],
                write_policy="legacy",
            )
            async with parent.session(context=context, environment=environment) as source:
                turn = await source.submit("Keep the executor ceiling.", tools=parent.tools)
                original = await turn.wait_for_request_or_result()
                assert isinstance(original, PendingRequests)
                await sibling.wait()
                [child] = source._configured_children
                opening = child.session._native_options

                def refuse():
                    raise RuntimeError("injected parent refusal")

                with pytest.raises(RuntimeError, match="injected parent refusal"):
                    await source._suspend(before_seal=refuse)
                [recovered] = source._configured_children
                # Inspect the replacement's current native selection before sampling resumes.
                before = json.loads(opening)["tool_policy"]["allowed_tools"]
                after = json.loads(recovered.session._native_options)["tool_policy"][
                    "allowed_tools"
                ]
                assert before == after
                assert "exec_command" not in {tool["name"] for tool in after}
                pending = await turn.wait_for_request_or_result()
                assert isinstance(pending, PendingRequests)
                [request] = pending.requests
                assert isinstance(request, ApprovalRequested)
                await source.respond(request.reply(True))
                assert (await turn.get_result()).final_output == "parent complete"
                assert "exec_command" not in str(child_model.requests[-1].get("tools", []))
            assert effects == ["accepted-sibling", "child-effect"]
            assert not executor.synthetic_process_starts
