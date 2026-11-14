"""Native parallel children have been retaining exact pending calls and usage."""

from contextlib import aclosing

import pytest

from _agents_codex.session import Session
from agents import Agent, ApprovalRequested, NativeProvider, SessionState, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_configured_agent_tool import MODEL, peer


async def pending_two(session):
    captured = []
    async with aclosing(session.interactions()) as interactions:
        async for request in interactions:
            assert isinstance(request, ApprovalRequested)
            captured.append(request)
            if len(captured) == 2:
                assert {item.call_id for item in captured} == {"inner_left", "inner_right"}
                return captured
    raise AssertionError("Both configured child approvals were not retained")


@pytest.mark.parametrize("mode", ["cold", "parent-refusal", "second-child-refusal"])
async def test_parallel_configured_checkpoint_keeps_pending_identity_and_usage(mode, monkeypatch):
    effects = []

    @function_tool(needs_approval=True)
    async def gated(value: str) -> str:
        effects.append(value)
        return value

    async with timeout(30), peer() as root, peer() as left, peer() as right:
        root.respond(
            function_call("left", {"input": "go"}, "outer_left"),
            function_call("right", {"input": "go"}, "outer_right"),
        )
        root.respond(assistant_output("parent done"))
        left.respond(function_call("gated", {"value": "left"}, "inner_left"))
        left.respond(assistant_output("left done"))
        right.respond(function_call("gated", {"value": "right"}, "inner_right"))
        right.respond(assistant_output("right done"))
        async with (
            NativeProvider(base_url=root.url, api_key="synthetic") as root_provider,
            NativeProvider(base_url=left.url, api_key="synthetic") as left_provider,
            NativeProvider(base_url=right.url, api_key="synthetic") as right_provider,
        ):

            def graph(changed=False):
                a = Agent(
                    name="left",
                    model=MODEL,
                    provider=left_provider,
                    tools=[gated],
                    instructions="Changed authority" if changed else "Left work",
                )
                b = Agent(
                    name="right",
                    model=MODEL,
                    provider=right_provider,
                    tools=[gated],
                    instructions="Right work",
                )
                return Agent(
                    instructions="Run both",
                    model=MODEL,
                    provider=root_provider,
                    tools=[a.as_tool("left"), b.as_tool("right")],
                )

            parent = graph()
            async with parent.session() as source:
                turn = await source.submit("go")
                await pending_two(source)
                assert not effects
                assert len(root.requests) == len(left.requests) == len(right.requests) == 1
                if mode == "cold":
                    saved = await source.suspend()
                    assert saved.native_continuation is not None
                    entries = saved.native_continuation.pending_manual
                    assert {item.call_id for item in entries} == {"outer_left", "outer_right"}
                    assert all(
                        item.continuation["kind"] == "configured-child-v1" for item in entries
                    )
                    assert Session._configured_checkpoint_usage(saved).requests == 3
                    cold = SessionState.from_json(saved.to_json())
                else:
                    if mode == "parent-refusal":

                        def refuse():
                            raise ValueError("parent preseal refusal")

                        with pytest.raises(ValueError, match="parent preseal refusal"):
                            await source._suspend(before_seal=refuse)
                    else:
                        failing = source._configured_children[1].session

                        async def refuse_child(_ready, *, _phase, **kwargs):
                            _phase.state = "refused"
                            raise ValueError("second child preseal refusal")

                        monkeypatch.setattr(failing, "_suspend_ready", refuse_child)
                        with pytest.raises(ValueError, match="second child preseal refusal"):
                            await source.suspend()
                    assert not effects
                    assert len(root.requests) == len(left.requests) == len(right.requests) == 1
                    for request in await pending_two(source):
                        await source.respond(request.reply(True))
                    assert (await turn.get_result()).final_output == "parent done"
                    assert turn.run_usage.requests == 6
                    assert turn.run_usage.total_tokens == 84

            if mode == "cold":
                # Changed current child authority cannot use either saved chain.
                async with graph(changed=True).session(state=cold) as blocked:
                    assert blocked.restored_turn is not None
                    with pytest.raises(
                        ValueError, match="Restore requires Agent.instructions to match"
                    ):
                        await blocked.restored_turn.get_result()
                assert not effects
                assert len(root.requests) == len(left.requests) == len(right.requests) == 1
                async with graph().session(state=cold) as restored:
                    assert restored.restored_turn is not None
                    assert restored.restored_turn.turn_id == turn.turn_id
                    for request in await pending_two(restored):
                        await restored.respond(request.reply(True))
                    assert (await restored.restored_turn.get_result()).final_output == "parent done"
                    assert restored.restored_turn.run_usage.requests == 6
                    assert restored.restored_turn.run_usage.total_tokens == 84

            assert sorted(effects) == ["left", "right"]
            assert len(root.requests) == len(left.requests) == len(right.requests) == 2
            outputs = [
                item
                for item in root.requests[1]["input"]
                if item.get("type") == "function_call_output"
            ]
            assert {item["call_id"] for item in outputs} == {"outer_left", "outer_right"}
            assert len(outputs) == 2
