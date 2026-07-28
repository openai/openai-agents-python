# Copyright

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from openai.types.responses.response_input_item_param import FunctionCallOutput

from agents import (
    Agent,
    FunctionToolResult,
    RunContextWrapper,
    Runner,
    ToolCallOutputItem,
    ToolOutputImage,
    ToolOutputText,
    ToolOutputTextDict,
    ToolsToFinalOutputResult,
    UserError,
    function_tool,
    tool_namespace,
)
from agents.run_internal import run_loop

from .fake_model import FakeModel
from .test_responses import get_function_tool, get_function_tool_call


def _make_function_tool_result(
    agent: Agent,
    output: str,
    tool_name: str | None = None,
    *,
    tool: Any | None = None,
) -> FunctionToolResult:
    # Construct a FunctionToolResult with the given output using a simple function tool.
    tool = tool or get_function_tool(tool_name or "dummy", return_value=output)
    raw_item: FunctionCallOutput = cast(
        FunctionCallOutput,
        {
            "call_id": "1",
            "output": output,
            "type": "function_call_output",
        },
    )
    # For this test we don't care about the specific RunItem subclass, only the output field
    run_item = ToolCallOutputItem(agent=agent, raw_item=raw_item, output=output)
    return FunctionToolResult(tool=tool, output=output, run_item=run_item)


@pytest.mark.asyncio
async def test_no_tool_results_returns_not_final_output() -> None:
    # If there are no tool results at all, tool_use_behavior should not produce a final output.
    agent = Agent(name="test")
    result = await run_loop.check_for_final_output_from_tools(
        agent=agent,
        tool_results=[],
        context_wrapper=RunContextWrapper(context=None),
    )
    assert result.is_final_output is False
    assert result.final_output is None


@pytest.mark.asyncio
async def test_run_llm_again_behavior() -> None:
    # With the default run_llm_again behavior, even with tools we still expect to keep running.
    agent = Agent(name="test", tool_use_behavior="run_llm_again")
    tool_results = [_make_function_tool_result(agent, "ignored")]
    result = await run_loop.check_for_final_output_from_tools(
        agent=agent,
        tool_results=tool_results,
        context_wrapper=RunContextWrapper(context=None),
    )
    assert result.is_final_output is False
    assert result.final_output is None


@pytest.mark.asyncio
async def test_stop_on_first_tool_behavior() -> None:
    # When tool_use_behavior is stop_on_first_tool, we should surface first tool output as final.
    agent = Agent(name="test", tool_use_behavior="stop_on_first_tool")
    tool_results = [
        _make_function_tool_result(agent, "first_tool_output"),
        _make_function_tool_result(agent, "ignored"),
    ]
    result = await run_loop.check_for_final_output_from_tools(
        agent=agent,
        tool_results=tool_results,
        context_wrapper=RunContextWrapper(context=None),
    )
    assert result.is_final_output is True
    assert result.final_output == "first_tool_output"


@pytest.mark.asyncio
async def test_custom_tool_use_behavior_sync() -> None:
    """If tool_use_behavior is a sync function, we should call it and propagate its return."""

    def behavior(
        context: RunContextWrapper, results: list[FunctionToolResult]
    ) -> ToolsToFinalOutputResult:
        assert len(results) == 3
        return ToolsToFinalOutputResult(is_final_output=True, final_output="custom")

    agent = Agent(name="test", tool_use_behavior=behavior)
    tool_results = [
        _make_function_tool_result(agent, "ignored1"),
        _make_function_tool_result(agent, "ignored2"),
        _make_function_tool_result(agent, "ignored3"),
    ]
    result = await run_loop.check_for_final_output_from_tools(
        agent=agent,
        tool_results=tool_results,
        context_wrapper=RunContextWrapper(context=None),
    )
    assert result.is_final_output is True
    assert result.final_output == "custom"


@pytest.mark.asyncio
async def test_custom_tool_use_behavior_async() -> None:
    """If tool_use_behavior is an async function, we should await it and propagate its return."""

    async def behavior(
        context: RunContextWrapper, results: list[FunctionToolResult]
    ) -> ToolsToFinalOutputResult:
        assert len(results) == 3
        return ToolsToFinalOutputResult(is_final_output=True, final_output="async_custom")

    agent = Agent(name="test", tool_use_behavior=behavior)
    tool_results = [
        _make_function_tool_result(agent, "ignored1"),
        _make_function_tool_result(agent, "ignored2"),
        _make_function_tool_result(agent, "ignored3"),
    ]
    result = await run_loop.check_for_final_output_from_tools(
        agent=agent,
        tool_results=tool_results,
        context_wrapper=RunContextWrapper(context=None),
    )
    assert result.is_final_output is True
    assert result.final_output == "async_custom"


@pytest.mark.asyncio
async def test_custom_tool_use_behavior_async_callable_object() -> None:
    """Async callable objects should be awaited and invoked exactly once."""

    class Behavior:
        def __init__(self) -> None:
            self.calls = 0

        async def __call__(
            self,
            context: RunContextWrapper,
            results: list[FunctionToolResult],
        ) -> ToolsToFinalOutputResult:
            self.calls += 1
            assert len(results) == 2
            return ToolsToFinalOutputResult(
                is_final_output=True,
                final_output="async_callable",
            )

    behavior = Behavior()
    agent = Agent(name="test", tool_use_behavior=behavior)
    tool_results = [
        _make_function_tool_result(agent, "ignored1"),
        _make_function_tool_result(agent, "ignored2"),
    ]
    result = await run_loop.check_for_final_output_from_tools(
        agent=agent,
        tool_results=tool_results,
        context_wrapper=RunContextWrapper(context=None),
    )

    assert result.is_final_output is True
    assert result.final_output == "async_callable"
    assert behavior.calls == 1


@pytest.mark.asyncio
async def test_invalid_tool_use_behavior_raises() -> None:
    """If tool_use_behavior is invalid, we should raise a UserError."""
    agent = Agent(name="test")
    # Force an invalid value; mypy will complain, so ignore the type here.
    agent.tool_use_behavior = "bad_value"  # type: ignore[assignment]
    tool_results = [_make_function_tool_result(agent, "ignored")]
    with pytest.raises(UserError):
        await run_loop.check_for_final_output_from_tools(
            agent=agent,
            tool_results=tool_results,
            context_wrapper=RunContextWrapper(context=None),
        )


@pytest.mark.asyncio
async def test_tool_names_to_stop_at_behavior() -> None:
    agent = Agent(
        name="test",
        tools=[
            get_function_tool("tool1", return_value="tool1_output"),
            get_function_tool("tool2", return_value="tool2_output"),
            get_function_tool("tool3", return_value="tool3_output"),
        ],
        tool_use_behavior={"stop_at_tool_names": ["tool1"]},
    )

    tool_results = [
        _make_function_tool_result(agent, "ignored1", "tool2"),
        _make_function_tool_result(agent, "ignored3", "tool3"),
    ]
    result = await run_loop.check_for_final_output_from_tools(
        agent=agent,
        tool_results=tool_results,
        context_wrapper=RunContextWrapper(context=None),
    )
    assert result.is_final_output is False, "We should not have stopped at tool1"

    # Now test with a tool that matches the list
    tool_results = [
        _make_function_tool_result(agent, "output1", "tool1"),
        _make_function_tool_result(agent, "ignored2", "tool2"),
        _make_function_tool_result(agent, "ignored3", "tool3"),
    ]
    result = await run_loop.check_for_final_output_from_tools(
        agent=agent,
        tool_results=tool_results,
        context_wrapper=RunContextWrapper(context=None),
    )
    assert result.is_final_output is True, "We should have stopped at tool1"
    assert result.final_output == "output1"


@pytest.mark.asyncio
async def test_stop_at_tool_names_supports_public_and_qualified_names_for_namespaced_tools() -> (
    None
):
    namespaced_tool = tool_namespace(
        name="billing",
        description="Billing tools",
        tools=[function_tool(lambda account_id: account_id, name_override="lookup_account")],
    )[0]
    agent = Agent(
        name="test",
        tools=[namespaced_tool],
        tool_use_behavior={"stop_at_tool_names": ["lookup_account"]},
    )

    tool_results = [
        _make_function_tool_result(agent, "billing-output", tool=namespaced_tool),
    ]
    result = await run_loop.check_for_final_output_from_tools(
        agent=agent,
        tool_results=tool_results,
        context_wrapper=RunContextWrapper(context=None),
    )
    assert result.is_final_output is True
    assert result.final_output == "billing-output"

    agent.tool_use_behavior = {"stop_at_tool_names": ["billing.lookup_account"]}
    result = await run_loop.check_for_final_output_from_tools(
        agent=agent,
        tool_results=tool_results,
        context_wrapper=RunContextWrapper(context=None),
    )
    assert result.is_final_output is True
    assert result.final_output == "billing-output"


@pytest.mark.asyncio
async def test_stop_on_first_tool_coerces_tool_output_text_to_plain_text() -> None:
    """Structured ToolOutputText must become its text, not a Pydantic __str__ repr."""

    @function_tool
    def text_tool() -> ToolOutputText:
        return ToolOutputText(text="hello structured")

    model = FakeModel()
    model.set_next_output([get_function_tool_call("text_tool", "{}")])
    agent = Agent(
        name="test",
        model=model,
        tools=[text_tool],
        tool_use_behavior="stop_on_first_tool",
    )

    result = await Runner.run(agent, "hi")
    assert result.final_output == "hello structured"
    assert "type=" not in result.final_output


@pytest.mark.asyncio
async def test_stop_on_first_tool_coerces_tool_output_text_dict_to_plain_text() -> None:
    @function_tool
    def text_tool() -> ToolOutputTextDict:
        return {"type": "text", "text": "hello from dict"}

    model = FakeModel()
    model.set_next_output([get_function_tool_call("text_tool", "{}")])
    agent = Agent(
        name="test",
        model=model,
        tools=[text_tool],
        tool_use_behavior="stop_on_first_tool",
    )

    result = await Runner.run(agent, "hi")
    assert result.final_output == "hello from dict"


@pytest.mark.asyncio
async def test_stop_at_tool_names_coerces_tool_output_text_to_plain_text() -> None:
    @function_tool
    def text_tool() -> ToolOutputText:
        return ToolOutputText(text="stopped structured")

    model = FakeModel()
    model.set_next_output([get_function_tool_call("text_tool", "{}")])
    agent = Agent(
        name="test",
        model=model,
        tools=[text_tool],
        tool_use_behavior={"stop_at_tool_names": ["text_tool"]},
    )

    result = await Runner.run(agent, "hi")
    assert result.final_output == "stopped structured"


@pytest.mark.asyncio
async def test_stop_on_first_tool_coerces_tool_output_image_without_pydantic_repr() -> None:
    @function_tool
    def image_tool() -> ToolOutputImage:
        return ToolOutputImage(image_url="data:image/png;base64,AAAA")

    model = FakeModel()
    model.set_next_output([get_function_tool_call("image_tool", "{}")])
    agent = Agent(
        name="test",
        model=model,
        tools=[image_tool],
        tool_use_behavior="stop_on_first_tool",
    )

    result = await Runner.run(agent, "hi")
    assert isinstance(result.final_output, str)
    assert "type='image'" not in result.final_output
    assert "image_url=" not in result.final_output
    parsed = json.loads(result.final_output)
    assert parsed == [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}]


@pytest.mark.asyncio
async def test_stop_on_first_tool_structured_output_streamed() -> None:
    @function_tool
    def text_tool() -> ToolOutputText:
        return ToolOutputText(text="streamed structured")

    model = FakeModel()
    model.set_next_output([get_function_tool_call("text_tool", "{}")])
    agent = Agent(
        name="test",
        model=model,
        tools=[text_tool],
        tool_use_behavior="stop_on_first_tool",
    )

    result = Runner.run_streamed(agent, "hi")
    async for _ in result.stream_events():
        pass
    assert result.final_output == "streamed structured"


@pytest.mark.asyncio
async def test_stop_on_first_tool_still_stringifies_non_structured_values() -> None:
    @function_tool
    def int_tool() -> int:
        return 42

    model = FakeModel()
    model.set_next_output([get_function_tool_call("int_tool", "{}")])
    agent = Agent(
        name="test",
        model=model,
        tools=[int_tool],
        tool_use_behavior="stop_on_first_tool",
    )

    result = await Runner.run(agent, "hi")
    assert result.final_output == "42"
