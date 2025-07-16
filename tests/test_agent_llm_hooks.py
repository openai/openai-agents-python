from collections import defaultdict
from typing import Any, Optional

import pytest

from agents.agent import Agent
from agents.items import ItemHelpers, ModelResponse, TResponseInputItem
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
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        response: ModelResponse,
    ) -> None:
        self.events["on_llm_end"] += 1


# Example test using the above hooks:
@pytest.mark.asyncio
async def test_async_agent_hooks_with_llm():
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


# test_sync_agent_hook_with_llm()
def test_sync_agent_hook_with_llm():
    hooks = AgentHooksForTests()
    model = FakeModel()
    agent = Agent(
        name="A", model=model, tools=[get_function_tool("f", "res")], handoffs=[], hooks=hooks
    )
    # Simulate a single LLM call producing an output:
    model.set_next_output([get_text_message("hello")])
    Runner.run_sync(agent, input="hello")
    # Expect one on_start, one on_llm_start, one on_llm_end, and one on_end
    assert hooks.events == {"on_start": 1, "on_llm_start": 1, "on_llm_end": 1, "on_end": 1}


# test_streamed_agent_hooks_with_llm():
@pytest.mark.asyncio
async def test_streamed_agent_hooks_with_llm():
    hooks = AgentHooksForTests()
    model = FakeModel()
    agent = Agent(
        name="A", model=model, tools=[get_function_tool("f", "res")], handoffs=[], hooks=hooks
    )
    # Simulate a single LLM call producing an output:
    model.set_next_output([get_text_message("hello")])
    stream = Runner.run_streamed(agent, input="hello")

    async for event in stream.stream_events():
        if event.type == "raw_response_event":
            continue
        if event.type == "agent_updated_stream_event":
            print(f"[EVENT] agent_updated → {event.new_agent.name}")
        elif event.type == "run_item_stream_event":
            item = event.item
            if item.type == "tool_call_item":
                print("[EVENT] tool_call_item")
            elif item.type == "tool_call_output_item":
                print(f"[EVENT] tool_call_output_item → {item.output}")
            elif item.type == "message_output_item":
                text = ItemHelpers.text_message_output(item)
                print(f"[EVENT] message_output_item → {text}")

    # Expect one on_start, one on_llm_start, one on_llm_end, and one on_end
    assert hooks.events == {"on_start": 1, "on_llm_start": 1, "on_llm_end": 1, "on_end": 1}
