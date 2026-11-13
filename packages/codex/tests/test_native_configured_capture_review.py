"""Configured capture ownership across passive cancellation and live rollback."""

import asyncio
import json
from dataclasses import replace

import pytest

from agents import (
    Agent,
    ApprovalRequested,
    CapabilityError,
    Environment,
    PendingRequests,
    TurnStoppedError,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import function_call
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
            suspend_child = child.session._suspend_ready
            sealed = asyncio.Event()
            release = asyncio.Event()
            calls = 0

            async def hold_after_native_seal(*args, **kwargs):
                nonlocal calls
                calls += 1
                saved = await suspend_child(*args, **kwargs)
                sealed.set()
                await release.wait()
                return saved

            monkeypatch.setattr(child.session, "_suspend_ready", hold_after_native_seal)
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


@pytest.mark.parametrize("stop", ["cancel", "close"])
async def test_configured_capture_leaves_child_readiness_cancellable(monkeypatch, stop):
    async with timeout(35), fixture() as values:
        parent, child_agent, context, _root_model, child_model, effects, _guards, sibling = values
        entered = asyncio.Event()
        release = asyncio.Event()
        retired = asyncio.Event()

        @function_tool
        async def wait_for_release() -> str:
            entered.set()
            try:
                await release.wait()
                return "released"
            finally:
                retired.set()

        child = replace(child_agent, tools=[*(child_agent.tools or []), wait_for_release])
        parent = replace(
            parent,
            tools=[child.as_tool("nested_child"), (parent.tools or [])[1]],
        )
        child_model.replies[0].append(function_call("wait_for_release", {}, "held-child-call"))
        async with parent.session(context=context) as source:
            turn = await source.submit("Wait at the configured child boundary.")
            await entered.wait()
            await sibling.wait()
            pending = await turn.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            [request] = pending.requests
            assert isinstance(request, ApprovalRequested)
            [owner] = source._configured_children
            wait_manual = owner.session._wait_manual_boundary
            waiting = asyncio.Event()

            async def observe_wait(*args, **kwargs):
                waiting.set()
                return await wait_manual(*args, **kwargs)

            monkeypatch.setattr(owner.session, "_wait_manual_boundary", observe_wait)
            capture = asyncio.create_task(source.suspend())
            closer = None
            try:
                await waiting.wait()
                if stop == "cancel":
                    capture.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        async with timeout(2):
                            await asyncio.shield(capture)
                    await source.respond(request.reply(True))
                    assert not retired.is_set()
                    release.set()
                    assert (await turn.get_result()).final_output == "parent complete"
                    assert effects == ["accepted-sibling", "child-effect"]
                else:
                    closer = asyncio.create_task(source.aclose())
                    async with timeout(5):
                        await asyncio.shield(closer)
                    with pytest.raises((RuntimeError, TurnStoppedError)):
                        await capture
                    assert retired.is_set()
                    assert effects == ["accepted-sibling"]
                assert source._sealed_checkpoint is None
                assert owner.session._sealed_checkpoint is None
            finally:
                release.set()
                await asyncio.gather(
                    capture, *([closer] if closer is not None else []), return_exceptions=True
                )


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
                assert opening is not None

                def refuse():
                    raise RuntimeError("injected parent refusal")

                with pytest.raises(RuntimeError, match="injected parent refusal"):
                    await source._suspend(before_seal=refuse)
                [recovered] = source._configured_children
                # Inspect the replacement's current native selection before sampling resumes.
                before = json.loads(opening)["tool_policy"]["allowed_tools"]
                recovered_options = recovered.session._native_options
                assert recovered_options is not None
                after = json.loads(recovered_options)["tool_policy"]["allowed_tools"]
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
