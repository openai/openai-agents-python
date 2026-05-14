"""Tests for ToolContext.send_progress and on_tool_progress hooks."""

from __future__ import annotations

from typing import Any

import pytest

from agents import Agent, AgentHooks, RunHooks, Runner, function_tool
from agents.tool import Tool
from agents.tool_context import ToolContext

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_text_message


class TestSendProgress:
    @pytest.mark.asyncio
    async def test_send_progress_fires_callback(self) -> None:
        """send_progress calls the _progress_fn when set."""
        received: list[Any] = []

        async def _on_progress(data: Any) -> None:
            received.append(data)

        ctx: ToolContext[None] = ToolContext(
            context=None,
            tool_name="test_tool",
            tool_call_id="call-1",
            tool_arguments="{}",
        )
        ctx.set_progress_fn(_on_progress)
        await ctx.send_progress({"status": "working"})
        assert received == [{"status": "working"}]

    @pytest.mark.asyncio
    async def test_send_progress_noop_without_fn(self) -> None:
        """send_progress is a no-op when _progress_fn is None."""
        ctx: ToolContext[None] = ToolContext(
            context=None,
            tool_name="test_tool",
            tool_call_id="call-1",
            tool_arguments="{}",
        )
        await ctx.send_progress({"status": "working"})

    @pytest.mark.asyncio
    async def test_send_progress_multiple_events(self) -> None:
        """Multiple send_progress calls all arrive in order."""
        received: list[Any] = []

        async def _on_progress(data: Any) -> None:
            received.append(data)

        ctx: ToolContext[None] = ToolContext(
            context=None,
            tool_name="test_tool",
            tool_call_id="call-1",
            tool_arguments="{}",
        )
        ctx.set_progress_fn(_on_progress)
        await ctx.send_progress({"step": 1})
        await ctx.send_progress({"step": 2})
        await ctx.send_progress({"step": 3})
        assert len(received) == 3
        assert received[0] == {"step": 1}
        assert received[1] == {"step": 2}
        assert received[2] == {"step": 3}


class _CollectingRunHooks(RunHooks[None]):
    def __init__(self) -> None:
        self.progress_events: list[dict[str, Any]] = []

    async def on_tool_progress(
        self,
        context: Any,
        agent: Any,
        tool: Tool,
        data: Any,
    ) -> None:
        self.progress_events.append({"tool_name": tool.name, "data": data})


class _CollectingAgentHooks(AgentHooks[None]):
    def __init__(self) -> None:
        self.progress_events: list[dict[str, Any]] = []

    async def on_tool_progress(
        self,
        context: Any,
        agent: Any,
        tool: Tool,
        data: Any,
    ) -> None:
        self.progress_events.append({"tool_name": tool.name, "data": data})


class TestHooksIntegration:
    @pytest.mark.asyncio
    async def test_run_hooks_fire_on_progress(self) -> None:
        """on_tool_progress fires on RunHooks during Runner.run()."""

        async def _progress_fn(ctx: ToolContext) -> str:
            await ctx.send_progress({"status": "working"})
            return "done"

        tool = function_tool(_progress_fn, name_override="progress_tool")
        hooks = _CollectingRunHooks()
        model = FakeModel()
        agent = Agent(name="test", model=model, tools=[tool])

        model.add_multiple_turn_outputs(
            [
                [get_function_tool_call("progress_tool", "{}")],
                [get_text_message("final")],
            ]
        )

        await Runner.run(agent, input="test", hooks=hooks)
        assert len(hooks.progress_events) == 1
        assert hooks.progress_events[0]["tool_name"] == "progress_tool"
        assert hooks.progress_events[0]["data"] == {"status": "working"}

    @pytest.mark.asyncio
    async def test_agent_hooks_fire_on_progress(self) -> None:
        """on_tool_progress fires on AgentHooks during Runner.run()."""

        async def _progress_fn(ctx: ToolContext) -> str:
            await ctx.send_progress({"status": "working"})
            return "done"

        tool = function_tool(_progress_fn, name_override="progress_tool")
        agent_hooks = _CollectingAgentHooks()
        model = FakeModel()
        agent = Agent(name="test", model=model, tools=[tool], hooks=agent_hooks)

        model.add_multiple_turn_outputs(
            [
                [get_function_tool_call("progress_tool", "{}")],
                [get_text_message("final")],
            ]
        )

        await Runner.run(agent, input="test")
        assert len(agent_hooks.progress_events) == 1
        assert agent_hooks.progress_events[0]["data"] == {"status": "working"}

    @pytest.mark.asyncio
    async def test_both_hooks_fire(self) -> None:
        """Both RunHooks and AgentHooks on_tool_progress fire."""

        async def _progress_fn(ctx: ToolContext) -> str:
            await ctx.send_progress({"status": "both"})
            return "done"

        tool = function_tool(_progress_fn, name_override="progress_tool")
        run_hooks = _CollectingRunHooks()
        agent_hooks = _CollectingAgentHooks()
        model = FakeModel()
        agent = Agent(name="test", model=model, tools=[tool], hooks=agent_hooks)

        model.add_multiple_turn_outputs(
            [
                [get_function_tool_call("progress_tool", "{}")],
                [get_text_message("final")],
            ]
        )

        await Runner.run(agent, input="test", hooks=run_hooks)
        assert len(run_hooks.progress_events) == 1
        assert len(agent_hooks.progress_events) == 1

    @pytest.mark.asyncio
    async def test_progress_in_streamed_run(self) -> None:
        """on_tool_progress hooks fire during Runner.run_streamed()."""

        async def _progress_fn(ctx: ToolContext) -> str:
            await ctx.send_progress({"status": "starting"})
            await ctx.send_progress({"status": "done"})
            return "result"

        tool = function_tool(_progress_fn, name_override="progress_tool")
        hooks = _CollectingRunHooks()
        model = FakeModel()
        agent = Agent(name="test", model=model, tools=[tool])

        model.add_multiple_turn_outputs(
            [
                [get_function_tool_call("progress_tool", "{}")],
                [get_text_message("final")],
            ]
        )

        result = Runner.run_streamed(agent, input="test", hooks=hooks)
        async for _ in result.stream_events():
            pass

        assert len(hooks.progress_events) == 2
        assert hooks.progress_events[0]["data"] == {"status": "starting"}
        assert hooks.progress_events[1]["data"] == {"status": "done"}

    @pytest.mark.asyncio
    async def test_parallel_tools_with_progress(self) -> None:
        """Parallel tools report progress with correct tool identity."""

        async def _tool_a(ctx: ToolContext) -> str:
            await ctx.send_progress({"tool": "a"})
            return "a_done"

        async def _tool_b(ctx: ToolContext) -> str:
            await ctx.send_progress({"tool": "b"})
            return "b_done"

        tool_a = function_tool(_tool_a, name_override="tool_a")
        tool_b = function_tool(_tool_b, name_override="tool_b")
        hooks = _CollectingRunHooks()
        model = FakeModel()
        agent = Agent(name="test", model=model, tools=[tool_a, tool_b])

        model.add_multiple_turn_outputs(
            [
                [
                    get_function_tool_call("tool_a", "{}", call_id="call_a"),
                    get_function_tool_call("tool_b", "{}", call_id="call_b"),
                ],
                [get_text_message("final")],
            ]
        )

        await Runner.run(agent, input="test", hooks=hooks)
        a_events = [e for e in hooks.progress_events if e["tool_name"] == "tool_a"]
        b_events = [e for e in hooks.progress_events if e["tool_name"] == "tool_b"]
        assert len(a_events) == 1
        assert len(b_events) == 1

    @pytest.mark.asyncio
    async def test_multiple_progress_events_in_order(self) -> None:
        """Multiple progress events arrive in emission order."""

        async def _progress_fn(ctx: ToolContext) -> str:
            await ctx.send_progress({"step": 1})
            await ctx.send_progress({"step": 2})
            await ctx.send_progress({"step": 3})
            return "done"

        tool = function_tool(_progress_fn, name_override="progress_tool")
        hooks = _CollectingRunHooks()
        model = FakeModel()
        agent = Agent(name="test", model=model, tools=[tool])

        model.add_multiple_turn_outputs(
            [
                [get_function_tool_call("progress_tool", "{}")],
                [get_text_message("final")],
            ]
        )

        await Runner.run(agent, input="test", hooks=hooks)
        assert [e["data"] for e in hooks.progress_events] == [
            {"step": 1},
            {"step": 2},
            {"step": 3},
        ]
