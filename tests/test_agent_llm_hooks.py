from collections import defaultdict
from typing import Any, Optional

import pytest

from agents.agent import Agent
from agents.items import ModelResponse, TResponseInputItem
from agents.lifecycle import AgentHooks
from agents.run import Runner
from agents.run_context import RunContextWrapper, TContext
from agents.tool import Tool

from .fake_model import FakeModel
from .test_responses import (
    get_function_tool,
    get_text_message,
)


class AgentHooksForTests(AgentHooks):
    def __init__(self):
        self.events: dict[str, int] = defaultdict(int)

    def reset(self):
        self.events.clear()

    async def on_start(self, context: RunContextWrapper[TContext], agent: Agent[TContext]) -> None:
        self.events["on_start"] += 1

    async def on_end(
        self, context: RunContextWrapper[TContext], agent: Agent[TContext], output: Any
    ) -> None:
        self.events["on_end"] += 1

    async def on_handoff(
        self, context: RunContextWrapper[TContext], agent: Agent[TContext], source: Agent[TContext]
    ) -> None:
        self.events["on_handoff"] += 1

    async def on_tool_start(
        self, context: RunContextWrapper[TContext], agent: Agent[TContext], tool: Tool
    ) -> None:
        self.events["on_tool_start"] += 1

    async def on_tool_end(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        tool: Tool,
        result: str,
    ) -> None:
        self.events["on_tool_end"] += 1

    # NEW: LLM hooks
    async def on_llm_start(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        system_prompt: Optional[str],
        input_items: list[TResponseInputItem],
    ) -> None:
        self.events["on_llm_start"] += 1

    async def on_llm_end(
        self,
        ccontext: RunContextWrapper[TContext],
        agent: Agent[TContext],
        response: ModelResponse,
    ) -> None:
        self.events["on_llm_end"] += 1


# Example test using the above hooks:
@pytest.mark.asyncio
async def test_non_streamed_agent_hooks_with_llm():
    hooks = AgentHooksForTests()
    model = FakeModel()
    agent = Agent(
        name="A", model=model, tools=[get_function_tool("f", "res")], handoffs=[], hooks=hooks
    )
    # Simulate a single LLM call producing an output:
    model.set_next_output([get_text_message("hello")])
    await Runner.run(agent, input="hello")
    # Expect one on_start, one on_llm_start, one on_llm_end, and one on_end
    assert hooks.events == {"on_start": 1, "on_llm_start": 1, "on_llm_end": 1, "on_end": 1}