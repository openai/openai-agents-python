from __future__ import annotations

from typing import Any, cast

import pytest
from openai.types.responses import (
    ResponseComputerToolCall,
    ResponseFileSearchToolCall,
    ResponseFunctionToolCall,
    ResponseFunctionWebSearch,
)
from openai.types.responses.response_computer_tool_call import ActionClick
from openai.types.responses.response_function_web_search import ActionSearch
from openai.types.responses.response_reasoning_item import ResponseReasoningItem, Summary
from pydantic import BaseModel

from agents import (
    Agent,
    Computer,
    ComputerTool,
    Handoff,
    HandoffInputData,
    ModelBehaviorError,
    ModelResponse,
    ReasoningItem,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    RunItem,
    ToolCallItem,
    Usage,
    handoff,
)
from agents.run_internal import run_loop
from agents.run_internal.run_loop import ToolRunHandoff, get_handoffs, get_output_schema

from .test_responses import (
    get_final_output_message,
    get_function_tool,
    get_function_tool_call,
    get_handoff_tool_call,
    get_text_input_item,
    get_text_message,
)


def _dummy_ctx() -> RunContextWrapper[None]:
    return RunContextWrapper(context=None)


async def process_response(
    agent: Agent[Any],
    response: ModelResponse,
    *,
    output_schema: Any = None,
    handoffs: list[Handoff[Any, Agent[Any]]] | None = None,
) -> Any:
    """Process a model response using the agent's tools and optional handoffs."""

    return run_loop.process_model_response(
        agent=agent,
        response=response,
        output_schema=output_schema,
        handoffs=handoffs or [],
        all_tools=await agent.get_all_tools(_dummy_ctx()),
    )


def test_empty_response():
    agent = Agent(name="test")
    response = ModelResponse(
        output=[],
        usage=Usage(),
        response_id=None,
    )

    result = run_loop.process_model_response(
        agent=agent,
        response=response,
        output_schema=None,
        handoffs=[],
        all_tools=[],
    )
    assert not result.handoffs
    assert not result.functions


def test_no_tool_calls():
    agent = Agent(name="test")
    response = ModelResponse(
        output=[get_text_message("Hello, world!")],
        usage=Usage(),
        response_id=None,
    )
    result = run_loop.process_model_response(
        agent=agent, response=response, output_schema=None, handoffs=[], all_tools=[]
    )
    assert not result.handoffs
    assert not result.functions


@pytest.mark.asyncio
async def test_single_tool_call():
    agent = Agent(name="test", tools=[get_function_tool(name="test")])
    response = ModelResponse(
        output=[
            get_text_message("Hello, world!"),
            get_function_tool_call("test", ""),
        ],
        usage=Usage(),
        response_id=None,
    )
    result = await process_response(agent=agent, response=response)
    assert not result.handoffs
    assert result.functions and len(result.functions) == 1

    func = result.functions[0]
    assert func.tool_call.name == "test"
    assert func.tool_call.arguments == ""


@pytest.mark.asyncio
async def test_missing_tool_call_raises_error():
    agent = Agent(name="test", tools=[get_function_tool(name="test")])
    response = ModelResponse(
        output=[
            get_text_message("Hello, world!"),
            get_function_tool_call("missing", ""),
        ],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(ModelBehaviorError):
        await process_response(agent=agent, response=response)


@pytest.mark.asyncio
async def test_multiple_tool_calls():
    agent = Agent(
        name="test",
        tools=[
            get_function_tool(name="test_1"),
            get_function_tool(name="test_2"),
            get_function_tool(name="test_3"),
        ],
    )
    response = ModelResponse(
        output=[
            get_text_message("Hello, world!"),
            get_function_tool_call("test_1", "abc"),
            get_function_tool_call("test_2", "xyz"),
        ],
        usage=Usage(),
        response_id=None,
    )

    result = await process_response(agent=agent, response=response)
    assert not result.handoffs
    assert result.functions and len(result.functions) == 2

    func_1 = result.functions[0]
    assert func_1.tool_call.name == "test_1"
    assert func_1.tool_call.arguments == "abc"

    func_2 = result.functions[1]
    assert func_2.tool_call.name == "test_2"
    assert func_2.tool_call.arguments == "xyz"


@pytest.mark.asyncio
async def test_handoffs_parsed_correctly():
    agent_1 = Agent(name="test_1")
    agent_2 = Agent(name="test_2")
    agent_3 = Agent(name="test_3", handoffs=[agent_1, agent_2])
    response = ModelResponse(
        output=[get_text_message("Hello, world!")],
        usage=Usage(),
        response_id=None,
    )
    result = await process_response(agent=agent_3, response=response)
    assert not result.handoffs, "Shouldn't have a handoff here"

    response = ModelResponse(
        output=[get_text_message("Hello, world!"), get_handoff_tool_call(agent_1)],
        usage=Usage(),
        response_id=None,
    )
    result = await process_response(
        agent=agent_3,
        response=response,
        handoffs=await get_handoffs(agent_3, _dummy_ctx()),
    )
    assert len(result.handoffs) == 1, "Should have a handoff here"
    handoff = result.handoffs[0]
    assert handoff.handoff.tool_name == Handoff.default_tool_name(agent_1)
    assert handoff.handoff.tool_description == Handoff.default_tool_description(agent_1)
    assert handoff.handoff.agent_name == agent_1.name

    handoff_agent = await handoff.handoff.on_invoke_handoff(
        RunContextWrapper(None), handoff.tool_call.arguments
    )
    assert handoff_agent == agent_1


@pytest.mark.asyncio
async def test_handoff_can_disable_run_level_history_nesting(monkeypatch: pytest.MonkeyPatch):
    source_agent = Agent(name="source")
    target_agent = Agent(name="target")
    override_handoff = handoff(target_agent, nest_handoff_history=False)
    tool_call = cast(ResponseFunctionToolCall, get_handoff_tool_call(target_agent))
    run_handoffs = [ToolRunHandoff(handoff=override_handoff, tool_call=tool_call)]
    run_config = RunConfig(nest_handoff_history=True)
    context_wrapper = RunContextWrapper(context=None)
    hooks = RunHooks()
    original_input = [get_text_input_item("hello")]
    pre_step_items: list[RunItem] = []
    new_step_items: list[RunItem] = []
    new_response = ModelResponse(output=[tool_call], usage=Usage(), response_id=None)

    calls: list[HandoffInputData] = []

    def fake_nest(
        handoff_input_data: HandoffInputData,
        *,
        history_mapper: Any,
    ) -> HandoffInputData:
        _ = history_mapper
        calls.append(handoff_input_data)
        return handoff_input_data

    monkeypatch.setattr("agents.run_internal.turn_resolution.nest_handoff_history", fake_nest)

    result = await run_loop.execute_handoffs(
        agent=source_agent,
        original_input=list(original_input),
        pre_step_items=pre_step_items,
        new_step_items=new_step_items,
        new_response=new_response,
        run_handoffs=run_handoffs,
        hooks=hooks,
        context_wrapper=context_wrapper,
        run_config=run_config,
    )

    assert calls == []
    assert result.original_input == original_input


@pytest.mark.asyncio
async def test_handoff_can_enable_history_nesting(monkeypatch: pytest.MonkeyPatch):
    source_agent = Agent(name="source")
    target_agent = Agent(name="target")
    override_handoff = handoff(target_agent, nest_handoff_history=True)
    tool_call = cast(ResponseFunctionToolCall, get_handoff_tool_call(target_agent))
    run_handoffs = [ToolRunHandoff(handoff=override_handoff, tool_call=tool_call)]
    run_config = RunConfig(nest_handoff_history=False)
    context_wrapper = RunContextWrapper(context=None)
    hooks = RunHooks()
    original_input = [get_text_input_item("hello")]
    pre_step_items: list[RunItem] = []
    new_step_items: list[RunItem] = []
    new_response = ModelResponse(output=[tool_call], usage=Usage(), response_id=None)

    def fake_nest(
        handoff_input_data: HandoffInputData,
        *,
        history_mapper: Any,
    ) -> HandoffInputData:
        _ = history_mapper
        return handoff_input_data.clone(
            input_history=(
                {
                    "role": "assistant",
                    "content": "nested",
                },
            )
        )

    monkeypatch.setattr("agents.run_internal.turn_resolution.nest_handoff_history", fake_nest)

    result = await run_loop.execute_handoffs(
        agent=source_agent,
        original_input=list(original_input),
        pre_step_items=pre_step_items,
        new_step_items=new_step_items,
        new_response=new_response,
        run_handoffs=run_handoffs,
        hooks=hooks,
        context_wrapper=context_wrapper,
        run_config=run_config,
    )

    assert result.original_input == [
        {
            "role": "assistant",
            "content": "nested",
        }
    ]


@pytest.mark.asyncio
async def test_missing_handoff_fails():
    agent_1 = Agent(name="test_1")
    agent_2 = Agent(name="test_2")
    agent_3 = Agent(name="test_3", handoffs=[agent_1])
    response = ModelResponse(
        output=[get_text_message("Hello, world!"), get_handoff_tool_call(agent_2)],
        usage=Usage(),
        response_id=None,
    )
    with pytest.raises(ModelBehaviorError):
        await process_response(
            agent=agent_3,
            response=response,
            handoffs=await get_handoffs(agent_3, _dummy_ctx()),
        )


@pytest.mark.asyncio
async def test_multiple_handoffs_doesnt_error():
    agent_1 = Agent(name="test_1")
    agent_2 = Agent(name="test_2")
    agent_3 = Agent(name="test_3", handoffs=[agent_1, agent_2])
    response = ModelResponse(
        output=[
            get_text_message("Hello, world!"),
            get_handoff_tool_call(agent_1),
            get_handoff_tool_call(agent_2),
        ],
        usage=Usage(),
        response_id=None,
    )
    result = await process_response(
        agent=agent_3,
        response=response,
        handoffs=await get_handoffs(agent_3, _dummy_ctx()),
    )
    assert len(result.handoffs) == 2, "Should have multiple handoffs here"


class Foo(BaseModel):
    bar: str


@pytest.mark.asyncio
async def test_final_output_parsed_correctly():
    agent = Agent(name="test", output_type=Foo)
    response = ModelResponse(
        output=[
            get_text_message("Hello, world!"),
            get_final_output_message(Foo(bar="123").model_dump_json()),
        ],
        usage=Usage(),
        response_id=None,
    )

    await process_response(
        agent=agent,
        response=response,
        output_schema=get_output_schema(agent),
    )


@pytest.mark.asyncio
async def test_file_search_tool_call_parsed_correctly():
    # Ensure that a ResponseFileSearchToolCall output is parsed into a ToolCallItem and that no tool
    # runs are scheduled.

    agent = Agent(name="test")
    file_search_call = ResponseFileSearchToolCall(
        id="fs1",
        queries=["query"],
        status="completed",
        type="file_search_call",
    )
    response = ModelResponse(
        output=[get_text_message("hello"), file_search_call],
        usage=Usage(),
        response_id=None,
    )
    result = await process_response(agent=agent, response=response)
    # The final item should be a ToolCallItem for the file search call
    assert any(
        isinstance(item, ToolCallItem) and item.raw_item is file_search_call
        for item in result.new_items
    )
    assert not result.functions
    assert not result.handoffs


@pytest.mark.asyncio
async def test_function_web_search_tool_call_parsed_correctly():
    agent = Agent(name="test")
    web_search_call = ResponseFunctionWebSearch(
        id="w1",
        action=ActionSearch(type="search", query="query"),
        status="completed",
        type="web_search_call",
    )
    response = ModelResponse(
        output=[get_text_message("hello"), web_search_call],
        usage=Usage(),
        response_id=None,
    )
    result = await process_response(agent=agent, response=response)
    assert any(
        isinstance(item, ToolCallItem) and item.raw_item is web_search_call
        for item in result.new_items
    )
    assert not result.functions
    assert not result.handoffs


@pytest.mark.asyncio
async def test_reasoning_item_parsed_correctly():
    # Verify that a Reasoning output item is converted into a ReasoningItem.

    reasoning = ResponseReasoningItem(
        id="r1", type="reasoning", summary=[Summary(text="why", type="summary_text")]
    )
    response = ModelResponse(
        output=[reasoning],
        usage=Usage(),
        response_id=None,
    )
    agent = Agent(name="test")
    result = await process_response(agent=agent, response=response)
    assert any(
        isinstance(item, ReasoningItem) and item.raw_item is reasoning for item in result.new_items
    )


class DummyComputer(Computer):
    """Minimal computer implementation for testing."""

    @property
    def environment(self):
        return "mac"  # pragma: no cover

    @property
    def dimensions(self):
        return (0, 0)  # pragma: no cover

    def screenshot(self) -> str:
        return ""  # pragma: no cover

    def click(self, x: int, y: int, button: str) -> None:
        return None  # pragma: no cover

    def double_click(self, x: int, y: int) -> None:
        return None  # pragma: no cover

    def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
        return None  # pragma: no cover

    def type(self, text: str) -> None:
        return None  # pragma: no cover

    def wait(self) -> None:
        return None  # pragma: no cover

    def move(self, x: int, y: int) -> None:
        return None  # pragma: no cover

    def keypress(self, keys: list[str]) -> None:
        return None  # pragma: no cover

    def drag(self, path: list[tuple[int, int]]) -> None:
        return None  # pragma: no cover


@pytest.mark.asyncio
async def test_computer_tool_call_without_computer_tool_raises_error():
    # If the agent has no ComputerTool in its tools, process_model_response should raise a
    # ModelBehaviorError when encountering a ResponseComputerToolCall.
    computer_call = ResponseComputerToolCall(
        id="c1",
        type="computer_call",
        action=ActionClick(type="click", x=1, y=2, button="left"),
        call_id="c1",
        pending_safety_checks=[],
        status="completed",
    )
    response = ModelResponse(
        output=[computer_call],
        usage=Usage(),
        response_id=None,
    )
    with pytest.raises(ModelBehaviorError):
        await process_response(agent=Agent(name="test"), response=response)


@pytest.mark.asyncio
async def test_computer_tool_call_with_computer_tool_parsed_correctly():
    # If the agent contains a ComputerTool, ensure that a ResponseComputerToolCall is parsed into a
    # ToolCallItem and scheduled to run in computer_actions.
    dummy_computer = DummyComputer()
    agent = Agent(name="test", tools=[ComputerTool(computer=dummy_computer)])
    computer_call = ResponseComputerToolCall(
        id="c1",
        type="computer_call",
        action=ActionClick(type="click", x=1, y=2, button="left"),
        call_id="c1",
        pending_safety_checks=[],
        status="completed",
    )
    response = ModelResponse(
        output=[computer_call],
        usage=Usage(),
        response_id=None,
    )
    result = await process_response(agent=agent, response=response)
    assert any(
        isinstance(item, ToolCallItem) and item.raw_item is computer_call
        for item in result.new_items
    )
    assert result.computer_actions and result.computer_actions[0].tool_call == computer_call


@pytest.mark.asyncio
async def test_tool_and_handoff_parsed_correctly():
    agent_1 = Agent(name="test_1")
    agent_2 = Agent(name="test_2")
    agent_3 = Agent(
        name="test_3", tools=[get_function_tool(name="test")], handoffs=[agent_1, agent_2]
    )
    response = ModelResponse(
        output=[
            get_text_message("Hello, world!"),
            get_function_tool_call("test", "abc"),
            get_handoff_tool_call(agent_1),
        ],
        usage=Usage(),
        response_id=None,
    )

    result = await process_response(
        agent=agent_3,
        response=response,
        handoffs=await get_handoffs(agent_3, _dummy_ctx()),
    )
    assert result.functions and len(result.functions) == 1
    assert len(result.handoffs) == 1, "Should have a handoff here"
    handoff = result.handoffs[0]
    assert handoff.handoff.tool_name == Handoff.default_tool_name(agent_1)
    assert handoff.handoff.tool_description == Handoff.default_tool_description(agent_1)
    assert handoff.handoff.agent_name == agent_1.name
