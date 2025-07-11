from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    MessageOutputItem,
    ModelResponse,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    RunItem,
    ToolCallItem,
    ToolCallOutputItem,
    TResponseInputItem,
    Usage,
)
from agents._run_impl import (
    NextStepFinalOutput,
    NextStepHandoff,
    NextStepRunAgain,
    RunImpl,
    SingleStepResult,
)
from agents.run import AgentRunner
from agents.tool import function_tool
from agents.tool_context import ToolContext

from .test_responses import (
    get_final_output_message,
    get_function_tool,
    get_function_tool_call,
    get_handoff_tool_call,
    get_text_input_item,
    get_text_message,
)


@pytest.mark.asyncio
async def test_empty_response_is_final_output():
    agent = Agent[None](name="test")
    response = ModelResponse(
        output=[],
        usage=Usage(),
        response_id=None,
    )
    result = await get_execute_result(agent, response)

    assert result.original_input == "hello"
    assert result.generated_items == []
    assert isinstance(result.next_step, NextStepFinalOutput)
    assert result.next_step.output == ""


@pytest.mark.asyncio
async def test_plaintext_agent_no_tool_calls_is_final_output():
    agent = Agent(name="test")
    response = ModelResponse(
        output=[get_text_message("hello_world")],
        usage=Usage(),
        response_id=None,
    )
    result = await get_execute_result(agent, response)

    assert result.original_input == "hello"
    assert len(result.generated_items) == 1
    assert_item_is_message(result.generated_items[0], "hello_world")
    assert isinstance(result.next_step, NextStepFinalOutput)
    assert result.next_step.output == "hello_world"


@pytest.mark.asyncio
async def test_plaintext_agent_no_tool_calls_multiple_messages_is_final_output():
    agent = Agent(name="test")
    response = ModelResponse(
        output=[
            get_text_message("hello_world"),
            get_text_message("bye"),
        ],
        usage=Usage(),
        response_id=None,
    )
    result = await get_execute_result(
        agent,
        response,
        original_input=[
            get_text_input_item("test"),
            get_text_input_item("test2"),
        ],
    )

    assert len(result.original_input) == 2
    assert len(result.generated_items) == 2
    assert_item_is_message(result.generated_items[0], "hello_world")
    assert_item_is_message(result.generated_items[1], "bye")

    assert isinstance(result.next_step, NextStepFinalOutput)
    assert result.next_step.output == "bye"


@pytest.mark.asyncio
async def test_plaintext_agent_with_tool_call_is_run_again():
    agent = Agent(name="test", tools=[get_function_tool(name="test", return_value="123")])
    response = ModelResponse(
        output=[get_text_message("hello_world"), get_function_tool_call("test", "")],
        usage=Usage(),
        response_id=None,
    )
    result = await get_execute_result(agent, response)

    assert result.original_input == "hello"

    # 3 items: new message, tool call, tool result
    assert len(result.generated_items) == 3
    assert isinstance(result.next_step, NextStepRunAgain)

    items = result.generated_items
    assert_item_is_message(items[0], "hello_world")
    assert_item_is_function_tool_call(items[1], "test", None)
    assert_item_is_function_tool_call_output(items[2], "123")

    assert isinstance(result.next_step, NextStepRunAgain)


@pytest.mark.asyncio
async def test_multiple_tool_calls():
    agent = Agent(
        name="test",
        tools=[
            get_function_tool(name="test_1", return_value="123"),
            get_function_tool(name="test_2", return_value="456"),
            get_function_tool(name="test_3", return_value="789"),
        ],
    )
    response = ModelResponse(
        output=[
            get_text_message("Hello, world!"),
            get_function_tool_call("test_1"),
            get_function_tool_call("test_2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    result = await get_execute_result(agent, response)
    assert result.original_input == "hello"

    # 5 items: new message, 2 tool calls, 2 tool call outputs
    assert len(result.generated_items) == 5
    assert isinstance(result.next_step, NextStepRunAgain)

    items = result.generated_items
    assert_item_is_message(items[0], "Hello, world!")
    assert_item_is_function_tool_call(items[1], "test_1", None)
    assert_item_is_function_tool_call(items[2], "test_2", None)

    assert isinstance(result.next_step, NextStepRunAgain)


@pytest.mark.asyncio
async def test_multiple_tool_calls_with_tool_context():
    async def _fake_tool(context: ToolContext[str], value: str) -> str:
        return f"{value}-{context.tool_call_id}"

    tool = function_tool(_fake_tool, name_override="fake_tool", failure_error_function=None)

    agent = Agent(
        name="test",
        tools=[tool],
    )
    response = ModelResponse(
        output=[
            get_function_tool_call("fake_tool", json.dumps({"value": "123"}), call_id="1"),
            get_function_tool_call("fake_tool", json.dumps({"value": "456"}), call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    result = await get_execute_result(agent, response)
    assert result.original_input == "hello"

    # 4 items: new message, 2 tool calls, 2 tool call outputs
    assert len(result.generated_items) == 4
    assert isinstance(result.next_step, NextStepRunAgain)

    items = result.generated_items
    assert_item_is_function_tool_call(items[0], "fake_tool", json.dumps({"value": "123"}))
    assert_item_is_function_tool_call(items[1], "fake_tool", json.dumps({"value": "456"}))
    assert_item_is_function_tool_call_output(items[2], "123-1")
    assert_item_is_function_tool_call_output(items[3], "456-2")

    assert isinstance(result.next_step, NextStepRunAgain)


@pytest.mark.asyncio
async def test_handoff_output_leads_to_handoff_next_step():
    agent_1 = Agent(name="test_1")
    agent_2 = Agent(name="test_2")
    agent_3 = Agent(name="test_3", handoffs=[agent_1, agent_2])
    response = ModelResponse(
        output=[get_text_message("Hello, world!"), get_handoff_tool_call(agent_1)],
        usage=Usage(),
        response_id=None,
    )
    result = await get_execute_result(agent_3, response)

    assert isinstance(result.next_step, NextStepHandoff)
    assert result.next_step.new_agent == agent_1

    assert len(result.generated_items) == 3


class Foo(BaseModel):
    bar: str


@pytest.mark.asyncio
async def test_final_output_without_tool_runs_again():
    agent = Agent(name="test", output_type=Foo, tools=[get_function_tool("tool_1", "result")])
    response = ModelResponse(
        output=[get_function_tool_call("tool_1")],
        usage=Usage(),
        response_id=None,
    )
    result = await get_execute_result(agent, response)

    assert isinstance(result.next_step, NextStepRunAgain)
    assert len(result.generated_items) == 2, "expected 2 items: tool call, tool call output"


@pytest.mark.asyncio
async def test_final_output_leads_to_final_output_next_step():
    agent = Agent(name="test", output_type=Foo)
    response = ModelResponse(
        output=[
            get_text_message("Hello, world!"),
            get_final_output_message(Foo(bar="123").model_dump_json()),
        ],
        usage=Usage(),
        response_id=None,
    )
    result = await get_execute_result(agent, response)

    assert isinstance(result.next_step, NextStepFinalOutput)
    assert result.next_step.output == Foo(bar="123")


@pytest.mark.asyncio
async def test_handoff_and_final_output_leads_to_handoff_next_step():
    agent_1 = Agent(name="test_1")
    agent_2 = Agent(name="test_2")
    agent_3 = Agent(name="test_3", handoffs=[agent_1, agent_2], output_type=Foo)
    response = ModelResponse(
        output=[
            get_final_output_message(Foo(bar="123").model_dump_json()),
            get_handoff_tool_call(agent_1),
        ],
        usage=Usage(),
        response_id=None,
    )
    result = await get_execute_result(agent_3, response)

    assert isinstance(result.next_step, NextStepHandoff)
    assert result.next_step.new_agent == agent_1


@pytest.mark.asyncio
async def test_multiple_final_output_leads_to_final_output_next_step():
    agent_1 = Agent(name="test_1")
    agent_2 = Agent(name="test_2")
    agent_3 = Agent(name="test_3", handoffs=[agent_1, agent_2], output_type=Foo)
    response = ModelResponse(
        output=[
            get_final_output_message(Foo(bar="123").model_dump_json()),
            get_final_output_message(Foo(bar="456").model_dump_json()),
        ],
        usage=Usage(),
        response_id=None,
    )
    result = await get_execute_result(agent_3, response)

    assert isinstance(result.next_step, NextStepFinalOutput)
    assert result.next_step.output == Foo(bar="456")


# === Helpers ===


def assert_item_is_message(item: RunItem, text: str) -> None:
    assert isinstance(item, MessageOutputItem)
    assert item.raw_item.type == "message"
    assert item.raw_item.role == "assistant"
    assert item.raw_item.content[0].type == "output_text"
    assert item.raw_item.content[0].text == text


def assert_item_is_function_tool_call(
    item: RunItem, name: str, arguments: str | None = None
) -> None:
    assert isinstance(item, ToolCallItem)
    assert item.raw_item.type == "function_call"
    assert item.raw_item.name == name
    assert not arguments or item.raw_item.arguments == arguments


def assert_item_is_function_tool_call_output(item: RunItem, output: str) -> None:
    assert isinstance(item, ToolCallOutputItem)
    assert item.raw_item["type"] == "function_call_output"
    assert item.raw_item["output"] == output


async def get_execute_result(
    agent: Agent[Any],
    response: ModelResponse,
    *,
    original_input: str | list[TResponseInputItem] | None = None,
    generated_items: list[RunItem] | None = None,
    hooks: RunHooks[Any] | None = None,
    context_wrapper: RunContextWrapper[Any] | None = None,
    run_config: RunConfig | None = None,
) -> SingleStepResult:
    output_schema = AgentRunner._get_output_schema(agent)
    handoffs = await AgentRunner._get_handoffs(agent, context_wrapper or RunContextWrapper(None))

    processed_response = RunImpl.process_model_response(
        agent=agent,
        all_tools=await agent.get_all_tools(context_wrapper or RunContextWrapper(None)),
        response=response,
        output_schema=output_schema,
        handoffs=handoffs,
    )
    return await RunImpl.execute_tools_and_side_effects(
        agent=agent,
        original_input=original_input or "hello",
        new_response=response,
        pre_step_items=generated_items or [],
        processed_response=processed_response,
        output_schema=output_schema,
        hooks=hooks or RunHooks(),
        context_wrapper=context_wrapper or RunContextWrapper(None),
        run_config=run_config or RunConfig(),
    )
