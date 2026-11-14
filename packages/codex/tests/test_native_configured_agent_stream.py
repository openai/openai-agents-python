from __future__ import annotations

import asyncio

import pytest

from agents import Agent, AgentToolStreamEvent, NativeProvider
from agents.observation import OutputTextDelta
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
