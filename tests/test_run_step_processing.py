from __future__ import annotations

import asyncio

import pytest
from openai.types.responses import (
    ResponseComputerToolCall,
    ResponseFileSearchToolCall,
    ResponseFunctionWebSearch,
)
from openai.types.responses.response_computer_tool_call import ActionClick
from openai.types.responses.response_reasoning_item import ResponseReasoningItem, Summary
from pydantic import BaseModel

from agents import (
    Agent,
    Computer,
    ComputerTool,
    Handoff,
    ModelBehaviorError,
    ModelResponse,
    ReasoningItem,
    RunContextWrapper,
    Runner,
    ToolCallItem,
    Usage,
)
from agents._run_impl import RunImpl
from agents.run import RunConfig
from agents.tracing.create import agent_span

from .test_responses import (
    get_final_output_message,
    get_function_tool,
    get_function_tool_call,
    get_handoff_tool_call,
    get_text_message,
)


def test_empty_response():
    agent = Agent(name="test")
    response = ModelResponse(
        output=[],
        usage=Usage(),
        response_id=None,
    )

    result = RunImpl.process_model_response(
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
    result = RunImpl.process_model_response(
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
    result = RunImpl.process_model_response(
        agent=agent,
        response=response,
        output_schema=None,
        handoffs=[],
        all_tools=await agent.get_all_tools(),
    )
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
        RunImpl.process_model_response(
            agent=agent,
            response=response,
            output_schema=None,
            handoffs=[],
            all_tools=await agent.get_all_tools(),
        )


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

    result = RunImpl.process_model_response(
        agent=agent,
        response=response,
        output_schema=None,
        handoffs=[],
        all_tools=await agent.get_all_tools(),
    )
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
    result = RunImpl.process_model_response(
        agent=agent_3,
        response=response,
        output_schema=None,
        handoffs=[],
        all_tools=await agent_3.get_all_tools(),
    )
    assert not result.handoffs, "Shouldn't have a handoff here"

    response = ModelResponse(
        output=[get_text_message("Hello, world!"), get_handoff_tool_call(agent_1)],
        usage=Usage(),
        response_id=None,
    )
    result = RunImpl.process_model_response(
        agent=agent_3,
        response=response,
        output_schema=None,
        handoffs=Runner._get_handoffs(agent_3),
        all_tools=await agent_3.get_all_tools(),
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
        RunImpl.process_model_response(
            agent=agent_3,
            response=response,
            output_schema=None,
            handoffs=Runner._get_handoffs(agent_3),
            all_tools=await agent_3.get_all_tools(),
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
    result = RunImpl.process_model_response(
        agent=agent_3,
        response=response,
        output_schema=None,
        handoffs=Runner._get_handoffs(agent_3),
        all_tools=await agent_3.get_all_tools(),
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

    RunImpl.process_model_response(
        agent=agent,
        response=response,
        output_schema=Runner._get_output_schema(agent),
        handoffs=[],
        all_tools=await agent.get_all_tools(),
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
    result = RunImpl.process_model_response(
        agent=agent,
        response=response,
        output_schema=None,
        handoffs=[],
        all_tools=await agent.get_all_tools(),
    )
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
    web_search_call = ResponseFunctionWebSearch(id="w1", status="completed", type="web_search_call")
    response = ModelResponse(
        output=[get_text_message("hello"), web_search_call],
        usage=Usage(),
        response_id=None,
    )
    result = RunImpl.process_model_response(
        agent=agent,
        response=response,
        output_schema=None,
        handoffs=[],
        all_tools=await agent.get_all_tools(),
    )
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
    result = RunImpl.process_model_response(
        agent=Agent(name="test"),
        response=response,
        output_schema=None,
        handoffs=[],
        all_tools=await Agent(name="test").get_all_tools(),
    )
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
        RunImpl.process_model_response(
            agent=Agent(name="test"),
            response=response,
            output_schema=None,
            handoffs=[],
            all_tools=await Agent(name="test").get_all_tools(),
        )


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
    result = RunImpl.process_model_response(
        agent=agent,
        response=response,
        output_schema=None,
        handoffs=[],
        all_tools=await agent.get_all_tools(),
    )
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

    result = RunImpl.process_model_response(
        agent=agent_3,
        response=response,
        output_schema=None,
        handoffs=Runner._get_handoffs(agent_3),
        all_tools=await agent_3.get_all_tools(),
    )
    assert result.functions and len(result.functions) == 1
    assert len(result.handoffs) == 1, "Should have a handoff here"
    handoff = result.handoffs[0]
    assert handoff.handoff.tool_name == Handoff.default_tool_name(agent_1)
    assert handoff.handoff.tool_description == Handoff.default_tool_description(agent_1)
    assert handoff.handoff.agent_name == agent_1.name


@pytest.fixture
def response_input_items():
    return [
        {  # Message
            "role": "system",
            "status": "completed",
            "type": "message",
            "content": "Message 1",
        },
        {  # Message
            "role": "user",
            "status": "completed",
            "type": "message",
            "content": "Message 2",
        },
        {  # ComputerCallOutput
            "call_id": "call_1",
            "output": {  # ResponseComputerToolCallOutputScreenshotParam
                "id": "screenshot_1",
                "type": "screenshot",
                "url": "http://example.com/screenshot.png",
            },
            "type": "computer_call_output",
            "id": "output_1",
            "acknowledged_safety_checks": [
                {  # ComputerCallOutputAcknowledgedSafetyCheck
                    "id": "check_1",
                    "code": "code_1",
                    "message": "message_1",
                }
            ],
        },
        {  # FunctionCallOutput
            "call_id": "call_2",
            "output": {  # ResponseFunctionWebSearch
                "id": "web_search_1",
                "type": "web_search_call",
                "status": "completed",
            },
            "type": "function_call_output",
            "id": "output_2",
        },
        {  # Message
            "role": "user",
            "status": "completed",
            "type": "message",
            "content": "Message 3",
        },
        {  # Message
            "role": "system",
            "status": "completed",
            "type": "message",
            "content": "Message 4",
        },
    ]


@pytest.fixture
def run_config():
    return RunConfig()


@pytest.fixture
def span():
    return agent_span(name="test_span")


@pytest.mark.asyncio
async def test_run_input_step_filter_not_callable(response_input_items, run_config, span):
    input_filter = "This is not callable"
    run_config.run_step_input_filter = input_filter

    # returns input by default
    result = await Runner._run_step_input_filter(
        original_input=response_input_items,
        run_config=run_config,
        span=span,
    )
    assert result == response_input_items

    # raises error if run_step_input_filter_raise_error is True
    run_config.run_step_input_filter_raise_error = True
    with pytest.raises(ModelBehaviorError):
        await Runner._run_step_input_filter(
            original_input=response_input_items,
            run_config=run_config,
            span=span,
        )


@pytest.mark.asyncio
async def test_run_input_step_filter_not_set(response_input_items, run_config, span):
    # returns input by default
    result = await Runner._run_step_input_filter(
        original_input=response_input_items,
        run_config=run_config,
        span=span,
    )
    assert result == response_input_items


@pytest.mark.asyncio
async def test_run_input_step_filter_output(response_input_items, run_config, span):
    # invalid output type
    def input_filter(*args, **kwargs):
        return 5

    run_config.run_step_input_filter = input_filter

    # returns input by default
    response = await Runner._run_step_input_filter(
        original_input=response_input_items,
        run_config=run_config,
        span=span,
    )
    assert response == response_input_items

    # raises error if run_step_input_filter_raise_error is True
    run_config.run_step_input_filter_raise_error = True
    with pytest.raises(ModelBehaviorError):
        await Runner._run_step_input_filter(
            original_input=response_input_items,
            run_config=run_config,
            span=span,
        )

    # string output is okay
    def input_filter_str_output(*args, **kwargs):
        return "This is a string output"

    run_config.run_step_input_filter = input_filter_str_output
    result = await Runner._run_step_input_filter(
        original_input=response_input_items,
        run_config=run_config,
        span=span,
    )
    assert result == "This is a string output"

    # list of dicts with "type"
    def input_filter_dict_output(*args, **kwargs):
        return [
            {"type": "message", "role": "user", "content": "This is a user message"},
            {"type": "message", "role": "system", "content": "This is a system message"},
        ]

    run_config.run_step_input_filter = input_filter_dict_output
    result = await Runner._run_step_input_filter(
        original_input=response_input_items,
        run_config=run_config,
        span=span,
    )
    assert len(result) == 2
    assert result == input_filter_dict_output()


@pytest.mark.asyncio
async def test_run_input_step_filter_error(response_input_items, run_config, span):
    def input_filter(*args, **kwargs):
        raise Exception("This is an error")

    run_config.run_step_input_filter = input_filter

    # returns input by default
    result = await Runner._run_step_input_filter(
        original_input=response_input_items,
        run_config=run_config,
        span=span,
    )
    assert result == response_input_items

    # raises error if run_step_input_filter_raise_error is True
    run_config.run_step_input_filter_raise_error = True
    with pytest.raises(ModelBehaviorError):
        await Runner._run_step_input_filter(
            original_input=response_input_items,
            run_config=run_config,
            span=span,
        )


@pytest.mark.asyncio
async def test_run_input_step_filter(response_input_items, run_config, span):
    # test sync function
    def input_filter(input_items):
        return [item for item in input_items if item.get("role", "") == "user"]

    run_config.run_step_input_filter = input_filter

    result = await Runner._run_step_input_filter(
        original_input=response_input_items,
        run_config=run_config,
        span=span,
    )
    assert all(item["role"] == "user" for item in result)
    assert len(result) == 2

    # test async function
    async def input_filter_async(input_items):
        return await asyncio.to_thread(
            lambda: [item for item in input_items if item.get("role", "") == "user"]
        )

    run_config.run_step_input_filter = input_filter_async
    result = await Runner._run_step_input_filter(
        original_input=response_input_items,
        run_config=run_config,
        span=span,
    )
    assert all(item["role"] == "user" for item in result)
    assert len(result) == 2
