from collections import defaultdict
from typing import Any, Optional, cast

import pytest

from agents.agent import Agent
from agents.items import ItemHelpers, ModelResponse, TResponseInputItem
from agents.lifecycle import AgentHooks, RunHooks
from agents.models.interface import Model
from agents.run import Runner
from agents.run_context import RunContextWrapper, TContext
from agents.tool import Tool
from tests.test_agent_llm_hooks import AgentHooksForTests

from .fake_model import FakeModel
from .test_responses import (
    get_function_tool,
    get_text_message,
)


class RunHooksForTests(RunHooks):
    def __init__(self):
        self.events: dict[str, int] = defaultdict(int)

    def reset(self):
        self.events.clear()

    async def on_agent_start(
        self, context: RunContextWrapper[TContext], agent: Agent[TContext]
    ) -> None:
        self.events["on_agent_start"] += 1

    async def on_agent_end(
        self, context: RunContextWrapper[TContext], agent: Agent[TContext], output: Any
    ) -> None:
        self.events["on_agent_end"] += 1

    async def on_handoff(
        self,
        context: RunContextWrapper[TContext],
        from_agent: Agent[TContext],
        to_agent: Agent[TContext],
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


# Example test using the above hooks
@pytest.mark.asyncio
async def test_async_run_hooks_with_llm():
    hooks = RunHooksForTests()
    model = FakeModel()

    agent = Agent(name="A", model=model, tools=[get_function_tool("f", "res")], handoffs=[])
    # Simulate a single LLM call producing an output:
    model.set_next_output([get_text_message("hello")])
    await Runner.run(agent, input="hello", hooks=hooks)
    # Expect one on_agent_start, one on_llm_start, one on_llm_end, and one on_agent_end
    assert hooks.events == {
        "on_agent_start": 1,
        "on_llm_start": 1,
        "on_llm_end": 1,
        "on_agent_end": 1,
    }


# test_sync_run_hook_with_llm()
def test_sync_run_hook_with_llm():
    hooks = RunHooksForTests()
    model = FakeModel()
    agent = Agent(name="A", model=model, tools=[get_function_tool("f", "res")], handoffs=[])
    # Simulate a single LLM call producing an output:
    model.set_next_output([get_text_message("hello")])
    Runner.run_sync(agent, input="hello", hooks=hooks)
    # Expect one on_agent_start, one on_llm_start, one on_llm_end, and one on_agent_end
    assert hooks.events == {
        "on_agent_start": 1,
        "on_llm_start": 1,
        "on_llm_end": 1,
        "on_agent_end": 1,
    }


# test_streamed_run_hooks_with_llm():
@pytest.mark.asyncio
async def test_streamed_run_hooks_with_llm():
    hooks = RunHooksForTests()
    model = FakeModel()
    agent = Agent(name="A", model=model, tools=[get_function_tool("f", "res")], handoffs=[])
    # Simulate a single LLM call producing an output:
    model.set_next_output([get_text_message("hello")])
    stream = Runner.run_streamed(agent, input="hello", hooks=hooks)

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

    # Expect one on_agent_start, one on_llm_start, one on_llm_end, and one on_agent_end
    assert hooks.events == {
        "on_agent_start": 1,
        "on_llm_start": 1,
        "on_llm_end": 1,
        "on_agent_end": 1,
    }


# test_async_run_hooks_with_agent_hooks_with_llm
@pytest.mark.asyncio
async def test_async_run_hooks_with_agent_hooks_with_llm():
    hooks = RunHooksForTests()
    agent_hooks = AgentHooksForTests()
    model = FakeModel()

    agent = Agent(
        name="A", model=model, tools=[get_function_tool("f", "res")], handoffs=[], hooks=agent_hooks
    )
    # Simulate a single LLM call producing an output:
    model.set_next_output([get_text_message("hello")])
    await Runner.run(agent, input="hello", hooks=hooks)
    # Expect one on_agent_start, one on_llm_start, one on_llm_end, and one on_agent_end
    assert hooks.events == {
        "on_agent_start": 1,
        "on_llm_start": 1,
        "on_llm_end": 1,
        "on_agent_end": 1,
    }
    # Expect one on_start, one on_llm_start, one on_llm_end, and one on_end
    assert agent_hooks.events == {"on_start": 1, "on_llm_start": 1, "on_llm_end": 1, "on_end": 1}


@pytest.mark.asyncio
async def test_run_hooks_llm_error_non_streaming(monkeypatch):
    hooks = RunHooksForTests()
    model = FakeModel()
    agent = Agent(name="A", model=model, tools=[get_function_tool("f", "res")], handoffs=[])

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(FakeModel, "get_response", boom, raising=True)

    with pytest.raises(RuntimeError, match="boom"):
        await Runner.run(agent, input="hello", hooks=hooks)

    # Current behavior is that hooks will not fire on LLM failure
    assert hooks.events["on_agent_start"] == 1
    assert hooks.events["on_llm_start"] == 1
    assert hooks.events["on_llm_end"] == 0
    assert hooks.events["on_agent_end"] == 0


class DummyAgentHooks(AgentHooks):
    """Agent-scoped hooks used to verify runtime validation."""


@pytest.mark.asyncio
async def test_runner_run_rejects_agent_hooks():
    model = FakeModel()
    agent = Agent(name="A", model=model)
    hooks = cast(RunHooks, DummyAgentHooks())

    with pytest.raises(TypeError, match="Run hooks must be instances of RunHooks"):
        await Runner.run(agent, input="hello", hooks=hooks)


def test_runner_run_streamed_rejects_agent_hooks():
    model = FakeModel()
    agent = Agent(name="A", model=model)
    hooks = cast(RunHooks, DummyAgentHooks())

    with pytest.raises(TypeError, match="Run hooks must be instances of RunHooks"):
        Runner.run_streamed(agent, input="hello", hooks=hooks)


class BoomModel(Model):
    async def get_response(self, *a, **k):
        raise AssertionError("get_response should not be called in streaming test")

    async def stream_response(self, *a, **k):
        yield {"foo": "bar"}
        raise RuntimeError("stream blew up")


@pytest.mark.asyncio
async def test_streamed_run_hooks_llm_error(monkeypatch):
    """
    Verify that when the streaming path raises, we still emit on_llm_start
    but do NOT emit on_llm_end (current behavior), and the exception propagates.
    """
    hooks = RunHooksForTests()
    agent = Agent(name="A", model=BoomModel(), tools=[get_function_tool("f", "res")], handoffs=[])

    stream = Runner.run_streamed(agent, input="hello", hooks=hooks)

    # Consuming the stream should surface the exception
    with pytest.raises(RuntimeError, match="stream blew up"):
        async for _ in stream.stream_events():
            pass

    # Current behavior: success-only on_llm_end; ensure starts fired but ends did not.
    assert hooks.events["on_agent_start"] == 1
    assert hooks.events["on_llm_start"] == 1
    assert hooks.events["on_llm_end"] == 0
    assert hooks.events["on_agent_end"] == 0
