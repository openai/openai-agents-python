from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any, cast

import pytest

from agents import Agent, Runner
from agents.items import InjectedInputItem, TResponseInputItem
from agents.lifecycle import AgentHooks, RunHooks
from agents.run_context import RunContextWrapper
from agents.stream_events import RunItemStreamEvent

from .fake_model import FakeModel
from .test_responses import (
    get_function_tool,
    get_function_tool_call,
    get_text_message,
)


class MessageInjectionHooks(RunHooks):
    def __init__(self, injected_text: str):
        self.injected_text = injected_text

    async def on_llm_start(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        _instructions: str | None,
        _input_items: list[TResponseInputItem],
    ) -> None:
        context.message_history.add_message(agent=agent, message=self.injected_text)


class OrderingAgentHooks(AgentHooks):
    def __init__(self) -> None:
        self.agent_start_text = "agent-start"
        self.before_tool_text = "before-tool"
        self.after_tool_text = "after-tool"

    async def on_start(self, context: RunContextWrapper[Any], agent: Agent[Any]) -> None:
        context.message_history.add_message(
            agent=agent,
            message={"role": "developer", "content": self.agent_start_text},
        )

    async def on_tool_start(
        self, context: RunContextWrapper[Any], agent: Agent[Any], tool: Any
    ) -> None:
        context.message_history.add_message(
            agent=agent,
            message={"role": "developer", "content": self.before_tool_text},
        )

    async def on_tool_end(
        self, context: RunContextWrapper[Any], agent: Agent[Any], tool: Any, result: Any
    ) -> None:
        context.message_history.add_message(
            agent=agent,
            message={"role": "developer", "content": self.after_tool_text},
        )


@pytest.mark.asyncio
async def test_run_hooks_can_inject_messages_into_llm_input() -> None:
    hooks = MessageInjectionHooks("Moderator: cite your sources.")
    model = FakeModel()
    model.set_next_output([get_text_message("done")])

    agent = Agent(name="editor", model=model)
    result = await Runner.run(agent, input="original prompt", hooks=hooks)

    assert model.last_turn_args["input"] == [
        {"role": "user", "content": "original prompt"},
        {"role": "user", "content": hooks.injected_text},
    ]

    injected_items = [item for item in result.new_items if isinstance(item, InjectedInputItem)]
    assert injected_items

    first_injected_raw = cast(MutableMapping[str, Any], injected_items[0].raw_item)
    assert first_injected_raw["content"] == hooks.injected_text


@pytest.mark.asyncio
async def test_streamed_runs_emit_injected_input_items() -> None:
    hooks = MessageInjectionHooks("Moderator: cite your sources.")
    model = FakeModel()
    model.set_next_output([get_text_message("done")])

    agent = Agent(name="editor", model=model)
    streamed_result = Runner.run_streamed(agent, input="streaming prompt", hooks=hooks)

    events: list[RunItemStreamEvent] = []
    async for event in streamed_result.stream_events():
        if isinstance(event, RunItemStreamEvent):
            events.append(event)

    assert model.last_turn_args["input"] == [
        {"role": "user", "content": "streaming prompt"},
        {"role": "user", "content": hooks.injected_text},
    ]

    injected_items = [
        item for item in streamed_result.new_items if isinstance(item, InjectedInputItem)
    ]
    assert injected_items
    assert any(isinstance(event.item, InjectedInputItem) for event in events)


def _find_index(items: list[TResponseInputItem], predicate) -> int:
    for idx, item in enumerate(items):
        if predicate(item):
            return idx
    raise AssertionError("predicate did not match any item")


@pytest.mark.asyncio
async def test_injected_messages_preserve_order_around_tool_calls() -> None:
    tool = get_function_tool(name="helper", return_value="ok")
    call_id = "call-123"
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call(tool.name, "{}", call_id=call_id)],
            [get_text_message("done")],
        ]
    )

    hooks = OrderingAgentHooks()
    agent = Agent(name="tester", model=model, tools=[tool], hooks=hooks)
    result = await Runner.run(agent, input="run")

    transcript = result.to_input_list()
    agent_start_idx = _find_index(
        transcript,
        lambda item: item.get("role") == "developer"
        and item.get("content") == hooks.agent_start_text,
    )
    before_tool_idx = _find_index(
        transcript,
        lambda item: item.get("role") == "developer"
        and item.get("content") == hooks.before_tool_text,
    )
    tool_output_idx = _find_index(
        transcript,
        lambda item: item.get("type") == "function_call_output" and item.get("call_id") == call_id,
    )
    after_tool_idx = _find_index(
        transcript,
        lambda item: item.get("role") == "developer"
        and item.get("content") == hooks.after_tool_text,
    )

    assert agent_start_idx < before_tool_idx < tool_output_idx < after_tool_idx
