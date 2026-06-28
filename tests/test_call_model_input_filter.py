from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from agents import Agent, RunConfig, RunContextWrapper, Runner, TResponseInputItem, UserError
from agents.run import CallModelData, ModelInputData
from agents.tool import function_tool

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_text_input_item, get_text_message


@dataclass
class ExternalEventContext:
    queued_user_messages: list[str] = field(default_factory=list)


EXTERNAL_MESSAGE = "The user added a new constraint while the tool was running."


@function_tool
def collect_external_update(wrapper: RunContextWrapper[ExternalEventContext]) -> str:
    wrapper.context.queued_user_messages.append(EXTERNAL_MESSAGE)
    return "tool-result"


def inject_queued_messages(data: CallModelData[ExternalEventContext]) -> ModelInputData:
    input_items = list(data.model_data.input)
    if data.context is not None:
        input_items.extend(
            get_text_input_item(message) for message in data.context.queued_user_messages
        )
        data.context.queued_user_messages.clear()
    return ModelInputData(input=input_items, instructions=data.model_data.instructions)


def assert_external_message_injected(input_items: list[TResponseInputItem]) -> None:
    last_item = cast(dict[str, Any], input_items[-1])
    assert last_item["content"] == EXTERNAL_MESSAGE
    assert any(item.get("type") == "function_call_output" for item in input_items)


@pytest.mark.asyncio
async def test_call_model_input_filter_sync_non_streamed() -> None:
    model = FakeModel()
    agent = Agent(name="test", model=model)

    # Prepare model output
    model.set_next_output([get_text_message("ok")])

    def filter_fn(data: CallModelData[Any]) -> ModelInputData:
        mi = data.model_data
        new_input = list(mi.input) + [get_text_input_item("added-sync")]
        return ModelInputData(input=new_input, instructions="filtered-sync")

    await Runner.run(
        agent,
        input="start",
        run_config=RunConfig(call_model_input_filter=filter_fn),
    )

    assert model.last_turn_args["system_instructions"] == "filtered-sync"
    assert isinstance(model.last_turn_args["input"], list)
    assert len(model.last_turn_args["input"]) == 2
    assert model.last_turn_args["input"][-1]["content"] == "added-sync"


@pytest.mark.asyncio
async def test_call_model_input_filter_async_streamed() -> None:
    model = FakeModel()
    agent = Agent(name="test", model=model)

    # Prepare model output
    model.set_next_output([get_text_message("ok")])

    async def filter_fn(data: CallModelData[Any]) -> ModelInputData:
        mi = data.model_data
        new_input = list(mi.input) + [get_text_input_item("added-async")]
        return ModelInputData(input=new_input, instructions="filtered-async")

    result = Runner.run_streamed(
        agent,
        input="start",
        run_config=RunConfig(call_model_input_filter=filter_fn),
    )
    async for _ in result.stream_events():
        pass

    assert model.last_turn_args["system_instructions"] == "filtered-async"
    assert isinstance(model.last_turn_args["input"], list)
    assert len(model.last_turn_args["input"]) == 2
    assert model.last_turn_args["input"][-1]["content"] == "added-async"


@pytest.mark.asyncio
async def test_call_model_input_filter_injects_external_input_between_tool_turns() -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("collect_external_update")],
            [get_text_message("done")],
        ]
    )
    agent = Agent[ExternalEventContext](
        name="test",
        model=model,
        tools=[collect_external_update],
    )
    context = ExternalEventContext()

    await Runner.run(
        agent,
        input="start",
        context=context,
        run_config=RunConfig(call_model_input_filter=inject_queued_messages),
    )

    assert isinstance(model.last_turn_args["input"], list)
    assert_external_message_injected(model.last_turn_args["input"])
    assert context.queued_user_messages == []


@pytest.mark.asyncio
async def test_call_model_input_filter_injects_external_input_between_streamed_tool_turns() -> None:
    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("collect_external_update")],
            [get_text_message("done")],
        ]
    )
    agent = Agent[ExternalEventContext](
        name="test",
        model=model,
        tools=[collect_external_update],
    )
    context = ExternalEventContext()

    result = Runner.run_streamed(
        agent,
        input="start",
        context=context,
        run_config=RunConfig(call_model_input_filter=inject_queued_messages),
    )
    async for _ in result.stream_events():
        pass

    assert isinstance(model.last_turn_args["input"], list)
    assert_external_message_injected(model.last_turn_args["input"])
    assert context.queued_user_messages == []


@pytest.mark.asyncio
async def test_call_model_input_filter_invalid_return_type_raises() -> None:
    model = FakeModel()
    agent = Agent(name="test", model=model)

    def invalid_filter(_data: CallModelData[Any]):
        return "bad"

    with pytest.raises(UserError):
        await Runner.run(
            agent,
            input="start",
            run_config=RunConfig(call_model_input_filter=invalid_filter),
        )


@pytest.mark.asyncio
async def test_call_model_input_filter_prefers_latest_duplicate_outputs_non_streamed() -> None:
    model = FakeModel()
    agent = Agent(name="test", model=model)
    model.set_next_output([get_text_message("ok")])

    duplicate_old = cast(
        TResponseInputItem,
        {
            "type": "function_call_output",
            "call_id": "dup-call",
            "output": "old-value",
        },
    )
    duplicate_new = cast(
        TResponseInputItem,
        {
            "type": "function_call_output",
            "call_id": "dup-call",
            "output": "new-value",
        },
    )

    def filter_fn(data: CallModelData[Any]) -> ModelInputData:
        return ModelInputData(
            input=[duplicate_old, duplicate_new] + list(data.model_data.input),
            instructions=data.model_data.instructions,
        )

    await Runner.run(
        agent,
        input="start",
        run_config=RunConfig(call_model_input_filter=filter_fn),
    )

    outputs = [
        item
        for item in model.last_turn_args["input"]
        if item.get("type") == "function_call_output" and item.get("call_id") == "dup-call"
    ]
    assert len(outputs) == 1
    assert outputs[0]["output"] == "new-value"


@pytest.mark.asyncio
async def test_call_model_input_filter_prefers_latest_duplicate_outputs_streamed() -> None:
    model = FakeModel()
    agent = Agent(name="test", model=model)
    model.set_next_output([get_text_message("ok")])

    duplicate_old = cast(
        TResponseInputItem,
        {
            "type": "function_call_output",
            "call_id": "dup-call-stream",
            "output": "old-value",
        },
    )
    duplicate_new = cast(
        TResponseInputItem,
        {
            "type": "function_call_output",
            "call_id": "dup-call-stream",
            "output": "new-value",
        },
    )

    async def filter_fn(data: CallModelData[Any]) -> ModelInputData:
        return ModelInputData(
            input=[duplicate_old, duplicate_new] + list(data.model_data.input),
            instructions=data.model_data.instructions,
        )

    result = Runner.run_streamed(
        agent,
        input="start",
        run_config=RunConfig(call_model_input_filter=filter_fn),
    )
    async for _ in result.stream_events():
        pass

    outputs = [
        item
        for item in model.last_turn_args["input"]
        if item.get("type") == "function_call_output" and item.get("call_id") == "dup-call-stream"
    ]
    assert len(outputs) == 1
    assert outputs[0]["output"] == "new-value"
