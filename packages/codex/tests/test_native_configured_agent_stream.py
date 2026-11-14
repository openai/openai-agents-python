from __future__ import annotations

import asyncio

import pytest

from agents import (
    Agent,
    AgentToolStreamEvent,
    ApprovalRequested,
    NativeProvider,
    PendingRequests,
    SessionState,
    function_tool,
)
from agents.observation import AgentProfileChanged, OutputTextDelta
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, text_response
from tests.test_native_configured_agent_tool import MODEL, peer


async def test_configured_child_streams_before_native_parent_continues():
    async with timeout(12), peer() as parent_model, peer() as child_model:
        parent_model.respond(function_call("specialist", {"input": "Work."}, "outer-call"))
        parent_model.respond(assistant_output("parent complete"))
        child_model.respond(*text_response("msg_stream_child", "child ", "complete"))
        entered = asyncio.Event()
        release = asyncio.Event()
        child_model.delta_gate = entered
        events: list[AgentToolStreamEvent] = []
        async with (
            NativeProvider(base_url=parent_model.url, api_key="synthetic-root") as root_provider,
            NativeProvider(base_url=child_model.url, api_key="synthetic-child") as child_provider,
        ):
            child = Agent(
                name="Specialist", instructions="Respond.", model=MODEL, provider=child_provider
            )

            async def observe(event: AgentToolStreamEvent) -> None:
                events.append(event)
                assert event.tool_call.call_id == "outer-call"
                if isinstance(event.event, OutputTextDelta):
                    entered.set()
                    await release.wait()

            parent = Agent(
                instructions="Use specialist.",
                model=MODEL,
                provider=root_provider,
                tools=[child.as_tool("specialist", on_stream=observe)],
            )
            task = asyncio.create_task(parent.run("Start."))
            try:
                await entered.wait()
                assert len(parent_model.requests) == 1
                assert not task.done()
                release.set()
                assert (await task).final_output == "parent complete"
                assert len(parent_model.requests) == 2
                assert all(event.agent is child for event in events)
                assert (
                    "".join(
                        event.event.delta
                        for event in events
                        if isinstance(event.event, OutputTextDelta)
                    )
                    == "child complete"
                )
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)


async def test_configured_child_sync_observer_errors_leave_native_result_unchanged(caplog):
    async with timeout(12), peer() as parent_model, peer() as child_model:
        parent_model.respond(function_call("specialist", {"input": "Work."}, "outer-call"))
        parent_model.respond(assistant_output("parent complete"))
        child_model.respond(*text_response("msg_stream_child", "child complete"))
        observed: list[str] = []
        async with (
            NativeProvider(base_url=parent_model.url, api_key="synthetic-root") as root_provider,
            NativeProvider(base_url=child_model.url, api_key="synthetic-child") as child_provider,
        ):
            child = Agent(
                name="Specialist", instructions="Respond.", model=MODEL, provider=child_provider
            )

            def observe(event: AgentToolStreamEvent) -> None:
                if isinstance(event.event, OutputTextDelta):
                    observed.append(event.event.delta)
                    raise ValueError("private callback text")

            with pytest.raises(ValueError, match="positive integer or None"):
                child.as_tool("specialist", on_stream=observe, on_stream_max_pending_events=0)

            parent = Agent(
                instructions="Use specialist.",
                model=MODEL,
                provider=root_provider,
                tools=[
                    child.as_tool(
                        "specialist", on_stream=observe, on_stream_max_pending_events=None
                    )
                ],
            )
            assert (await parent.run("Start.")).final_output == "parent complete"
            assert observed == ["child complete"]
            assert "on_stream callback failed" in caplog.text
            assert "private callback text" not in caplog.text


async def test_configured_child_stream_overflow_joins_the_active_callback():
    async with timeout(12), peer() as parent_model, peer() as child_model:
        parent_model.respond(function_call("specialist", {"input": "Work."}, "outer-call"))
        child_model.respond(*text_response("msg_stream_child", *[str(i) for i in range(10)]))
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        async with (
            NativeProvider(base_url=parent_model.url, api_key="synthetic-root") as root_provider,
            NativeProvider(base_url=child_model.url, api_key="synthetic-child") as child_provider,
        ):
            child = Agent(
                name="Specialist", instructions="Respond.", model=MODEL, provider=child_provider
            )

            async def block(_event: AgentToolStreamEvent) -> None:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            parent = Agent(
                instructions="Use specialist.",
                model=MODEL,
                provider=root_provider,
                tools=[
                    child.as_tool("specialist", on_stream=block, on_stream_max_pending_events=1)
                ],
            )
            with pytest.raises(Exception, match="on_stream backlog exceeded"):
                await parent.run("Start.")
            assert entered.is_set() and cancelled.is_set()
            assert len(parent_model.requests) == 1


@pytest.mark.parametrize("parent_handoff", [False, True])
async def test_configured_child_stream_uses_the_restored_handoff_destination(parent_handoff):
    async with timeout(20), peer() as parent_model, peer() as child_model:
        if parent_handoff:
            parent_model.respond(function_call("transfer_to_coordinator", {}, "parent-transfer"))
        parent_model.respond(function_call("specialist", {"input": "Work."}, "outer-call"))
        parent_model.respond(assistant_output("parent complete"))
        child_model.respond(function_call("transfer_to_destination", {}, "transfer"))
        child_model.respond(function_call("gated", {}, "gated-call"))
        child_model.respond(*text_response("msg_after_approval", "approved child"))
        effects: list[str] = []
        observed: list[AgentToolStreamEvent] = []

        @function_tool(needs_approval=True)
        async def gated() -> str:
            effects.append("gated")
            return "effect"

        async with (
            NativeProvider(base_url=parent_model.url, api_key="synthetic-root") as root_provider,
            NativeProvider(base_url=child_model.url, api_key="synthetic-child") as child_provider,
        ):
            destination = Agent(
                name="destination",
                instructions="Ask for approval.",
                model=MODEL,
                provider=child_provider,
                tools=[gated],
            )
            child = Agent(
                name="specialist",
                instructions="Transfer to destination.",
                model=MODEL,
                provider=child_provider,
                handoffs=[destination],
            )

            def root_using(configured):
                coordinator = Agent(
                    name="coordinator",
                    instructions="Use specialist.",
                    model=MODEL,
                    provider=root_provider,
                    tools=[configured.as_tool("specialist", on_stream=observed.append)],
                )
                if not parent_handoff:
                    return coordinator
                return Agent(
                    name="front door",
                    instructions="Transfer to coordinator.",
                    model=MODEL,
                    provider=root_provider,
                    handoffs=[coordinator],
                )

            parent = root_using(child)
            async with parent.session() as source:
                turn = await source.submit("Start.")
                pending = await turn.wait_for_request_or_result()
                assert isinstance(pending, PendingRequests)
                checkpoint = await source.suspend()
            assert not effects
            changed = [event for event in observed if isinstance(event.event, AgentProfileChanged)]
            assert len(changed) == 1 and changed[0].agent is destination
            observed.clear()
            checkpoint = SessionState.from_json(checkpoint.to_json())
            changed_destination = Agent(
                name="destination",
                instructions="Changed handoff authority.",
                model=MODEL,
                provider=child_provider,
                tools=[gated],
            )
            changed_child = Agent(
                name="specialist",
                instructions="Transfer to destination.",
                model=MODEL,
                provider=child_provider,
                handoffs=[changed_destination],
            )
            changed_parent = root_using(changed_child)
            async with changed_parent.session(state=checkpoint) as changed:
                assert changed.restored_turn is not None
                with pytest.raises(RuntimeError, match="(fingerprint|Agent declaration graph)"):
                    await changed.restored_turn.get_result()
            assert not effects and not observed
            assert len(parent_model.requests) == 1 + parent_handoff
            assert len(child_model.requests) == 2

            async with parent.session(state=checkpoint) as fresh:
                restored = fresh.restored_turn
                assert restored is not None
                pending = await restored.wait_for_request_or_result()
                assert isinstance(pending, PendingRequests)
                [request] = pending.requests
                assert isinstance(request, ApprovalRequested)
                await fresh.respond(request.reply(True))
                assert (await restored.get_result()).final_output == "parent complete"

            deltas = [event for event in observed if isinstance(event.event, OutputTextDelta)]
            assert len(deltas) == 1
            assert deltas[0].event.delta == "approved child"
            assert deltas[0].agent is destination and deltas[0].tool_call.call_id == "outer-call"
            assert effects == ["gated"]
            assert len(parent_model.requests) == 2 + parent_handoff
            assert len(child_model.requests) == 3
