"""Tests for on_tool_authorize lifecycle hook (issue #2868)."""

from __future__ import annotations

from typing import Any

import pytest

from agents import Agent, Runner
from agents.lifecycle import AgentHooks, RunHooks
from agents.run_context import RunContextWrapper, TContext
from agents.tool import FunctionTool, Tool

from .fake_model import FakeModel
from .test_responses import get_function_tool, get_function_tool_call, get_text_message


DENIAL_MSG = "Tool call denied: authorization hook returned False."


class AllowRunHooks(RunHooks):
    """Always authorizes tool calls; records invocations."""

    def __init__(self) -> None:
        """Initialise empty tracking lists."""
        self.authorize_calls: list[str] = []
        self.start_calls: list[str] = []
        self.end_calls: list[str] = []

    async def on_tool_authorize(
        self, context: Any, agent: Any, tool: Any
    ) -> bool:
        """Record and authorize the tool call."""
        self.authorize_calls.append(tool.name)
        return True

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        """Record that the tool started."""
        self.start_calls.append(tool.name)

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        """Record that the tool ended."""
        self.end_calls.append(tool.name)


class DenyRunHooks(RunHooks):
    """Denies all tool calls."""

    def __init__(self) -> None:
        """Initialise empty tracking lists."""
        self.authorize_calls: list[str] = []
        self.start_calls: list[str] = []
        self.end_calls: list[str] = []

    async def on_tool_authorize(
        self, context: Any, agent: Any, tool: Any
    ) -> bool:
        """Record and deny the tool call."""
        self.authorize_calls.append(tool.name)
        return False

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        """Record that the tool started (should not be called when denied)."""
        self.start_calls.append(tool.name)

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        """Record that the tool ended (should not be called when denied)."""
        self.end_calls.append(tool.name)


class DenyAgentHooks(AgentHooks):
    """Agent-level deny hook."""

    def __init__(self) -> None:
        """Initialise empty tracking list."""
        self.authorize_calls: list[str] = []

    async def on_tool_authorize(
        self, context: Any, agent: Any, tool: Any
    ) -> bool:
        """Record and deny the tool call."""
        self.authorize_calls.append(tool.name)
        return False


@pytest.mark.asyncio
async def test_allow_hook_lets_tool_run() -> None:
    """When on_tool_authorize returns True the tool executes normally."""
    tool = get_function_tool("my_tool", "tool_result")
    model = FakeModel()
    model.add_multiple_turn_outputs([
        [get_function_tool_call("my_tool", "{}")],
        [get_text_message("done")],
    ])

    hooks = AllowRunHooks()
    agent = Agent(name="A", model=model, tools=[tool])
    result = await Runner.run(agent, input="hi", hooks=hooks)

    assert hooks.authorize_calls == ["my_tool"]
    assert hooks.start_calls == ["my_tool"]
    assert hooks.end_calls == ["my_tool"]
    assert result.final_output == "done"


@pytest.mark.asyncio
async def test_deny_hook_skips_tool_execution() -> None:
    """When on_tool_authorize returns False the tool is not executed and model gets denial."""
    invoked: list[bool] = []

    async def my_tool_impl(ctx: Any, args: str) -> str:
        """Append True to invoked and return a sentinel string (should never be called)."""
        invoked.append(True)
        return "should not be returned"

    # Wire my_tool_impl directly into FunctionTool so we can assert it was never called.
    func_tool = FunctionTool(
        name="my_tool",
        description="test tool",
        params_json_schema={},
        on_invoke_tool=my_tool_impl,
        strict_json_schema=False,
    )
    model = FakeModel()
    model.add_multiple_turn_outputs([
        [get_function_tool_call("my_tool", "{}")],
        [get_text_message("done")],
    ])

    hooks = DenyRunHooks()
    agent = Agent(name="A", model=model, tools=[func_tool])
    result = await Runner.run(agent, input="hi", hooks=hooks)

    # The authorization hook was called
    assert hooks.authorize_calls == ["my_tool"]
    # But on_tool_start and on_tool_end were NOT called (denied before them)
    assert hooks.start_calls == []
    assert hooks.end_calls == []
    # And the run still completes (model sees the denial and produces final output)
    assert result.final_output == "done"
    # Verify my_tool_impl was never actually invoked
    assert invoked == []


@pytest.mark.asyncio
async def test_deny_hook_sends_denial_message_to_model() -> None:
    """The model receives the denial string as the tool output."""
    received_tool_outputs: list[str] = []

    class OutputCapturingHooks(RunHooks):
        """Captures tool outputs by denying every tool call."""

        async def on_tool_authorize(self, context: Any, agent: Any, tool: Any) -> bool:
            """Deny every tool call unconditionally."""
            return False

    model = FakeModel()
    func_tool = get_function_tool("my_tool", "real_result")
    model.add_multiple_turn_outputs([
        [get_function_tool_call("my_tool", "{}")],
        [get_text_message("done")],
    ])

    hooks = OutputCapturingHooks()
    agent = Agent(name="A", model=model, tools=[func_tool])
    result = await Runner.run(agent, input="hi", hooks=hooks)

    # Check that model received denial message in its input on second turn.
    # The denial string is stored as the tool output in new_items.
    raw_responses = result.raw_responses
    assert len(raw_responses) >= 1
    # The denial message must appear as a ToolCallOutputItem in the run's new items.
    tool_outputs = [
        str(item.output)
        for item in result.new_items
        if hasattr(item, "output") and item.output is not None
    ]
    assert any(DENIAL_MSG in output for output in tool_outputs), (
        f"Expected denial message {DENIAL_MSG!r} in tool outputs, got {tool_outputs}"
    )
    assert result.final_output == "done"


@pytest.mark.asyncio
async def test_agent_level_deny_hook() -> None:
    """Agent-level on_tool_authorize returning False also denies the call."""
    func_tool = get_function_tool("blocked_tool", "should_not_run")
    model = FakeModel()
    model.add_multiple_turn_outputs([
        [get_function_tool_call("blocked_tool", "{}")],
        [get_text_message("fine")],
    ])

    agent_hooks = DenyAgentHooks()
    agent = Agent(name="A", model=model, tools=[func_tool], hooks=agent_hooks)
    result = await Runner.run(agent, input="hi")

    assert agent_hooks.authorize_calls == ["blocked_tool"]
    assert result.final_output == "fine"


@pytest.mark.asyncio
async def test_authorize_not_called_when_no_tool_used() -> None:
    """on_tool_authorize is not called when the model produces a final output directly."""
    model = FakeModel()
    model.set_next_output([get_text_message("hello")])

    hooks = AllowRunHooks()
    agent = Agent(name="A", model=model)
    await Runner.run(agent, input="hi", hooks=hooks)

    assert hooks.authorize_calls == []
    assert hooks.start_calls == []


@pytest.mark.asyncio
async def test_default_hook_allows_all() -> None:
    """The default RunHooks implementation allows all tool calls (no override needed)."""
    func_tool = get_function_tool("calc", "42")
    model = FakeModel()
    model.add_multiple_turn_outputs([
        [get_function_tool_call("calc", "{}")],
        [get_text_message("answer is 42")],
    ])

    # Use the base class without overriding on_tool_authorize
    agent = Agent(name="A", model=model, tools=[func_tool])
    result = await Runner.run(agent, input="hi")

    assert result.final_output == "answer is 42"
