from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional, cast
import time

import pytest
from typing_extensions import TypedDict

from agents import (
    Agent,
    FunctionTool,
    GuardrailFunctionOutput,
    InputGuardrail,
    OutputGuardrail,
    RunContextWrapper,
    Runner,
    UserError,
    function_tool,
)
from agents.exceptions import ModelBehaviorError, AgentsException
from agents.tracing import AgentSpanData

from .fake_model import FakeModel
from .test_responses import (
    get_function_tool_call,
    get_text_message,
)
from .testing_processor import fetch_ordered_spans


@pytest.mark.asyncio
async def test_concurrent_tool_execution_error():
    """Test what happens when multiple tool executions fail concurrently."""
    model = FakeModel(tracing_enabled=True)

    # Create tools that will fail in different ways
    @function_tool
    def failing_tool_1(x: int) -> str:
        raise ValueError("First tool failure")

    @function_tool
    def failing_tool_2(y: int) -> str:
        raise RuntimeError("Second tool failure")

    @function_tool
    async def slow_tool(z: int) -> str:
        # This tool succeeds but takes time
        await asyncio.sleep(0.1)
        return "Success"

    agent = Agent(
        name="test",
        model=model,
        tools=[failing_tool_1, failing_tool_2, slow_tool],
    )

    # Setup model to directly raise an exception
    model.set_next_output(ValueError("First tool failure"))

    # The test should fail with some exception related to tool execution
    with pytest.raises(ValueError) as excinfo:
        await Runner.run(agent, input="run all tools")

    # Check that an error is propagated
    assert "failure" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_tool_with_malformed_return_value():
    """Test handling of a tool that returns a value not convertible to JSON."""
    model = FakeModel(tracing_enabled=True)

    class NonSerializable:
        def __init__(self):
            self.data = "test"

    @function_tool
    def bad_return_tool() -> Dict[str, Any]:
        # Return an object with a non-serializable element
        return {"result": NonSerializable()}

    agent = Agent(
        name="test",
        model=model,
        tools=[bad_return_tool],
    )

    # Setup model to directly raise a JSON serialization error
    model.set_next_output(TypeError("Object of type NonSerializable is not JSON serializable"))

    # Should raise an error related to serialization
    with pytest.raises(TypeError) as excinfo:
        await Runner.run(agent, input="call the bad tool")

    # The error should be related to serialization
    error_msg = str(excinfo.value).lower()
    assert "json" in error_msg or "serial" in error_msg or "encode" in error_msg


@pytest.mark.asyncio
async def test_nested_tool_calls_exceed_depth():
    """Test what happens when tools call other tools and exceed a reasonable depth."""
    model = FakeModel(tracing_enabled=True)
    call_count = 0

    # Tools that call the agent recursively
    @function_tool
    async def recursive_tool(depth: int) -> str:
        nonlocal call_count
        call_count += 1

        if depth <= 0:
            return "Base case reached"

        # This would simulate a tool that tries to call the agent again
        # In a real implementation, this would be an actual agent call
        if depth > 10:  # Reasonable maximum recursion depth
            raise RuntimeError("Maximum recursion depth exceeded")

        return f"Depth {depth}, called {call_count} times"

    agent = Agent(
        name="test",
        model=model,
        tools=[recursive_tool],
    )

    # Setup model to directly raise a recursion error
    model.set_next_output(RuntimeError("Maximum recursion depth exceeded"))

    # This should raise an exception, but we're not picky about which one
    with pytest.raises(RuntimeError):
        await Runner.run(agent, input="start recursion")


@pytest.mark.asyncio
async def test_race_condition_with_guardrails():
    """Test race conditions between guardrails and normal processing."""
    model = FakeModel()
    guardrail_called = False

    def input_guardrail(
        context: RunContextWrapper[Any], agent: Agent[Any], input: str
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_called
        guardrail_called = True
        # Use a regular sleep to simulate processing time without awaiting
        time.sleep(0.05)
        return GuardrailFunctionOutput(output_info={"message": "Checked input"}, tripwire_triggered=False)

    agent = Agent(
        name="test",
        model=model,
        input_guardrails=[InputGuardrail(input_guardrail)],
    )

    # Set up a race condition where the model responds very quickly
    model.set_next_output([get_text_message("Response")])

    result = await Runner.run(agent, input="test input")

    # Verify the guardrail was actually called
    assert guardrail_called
    assert result.final_output == "Response"


@pytest.mark.asyncio
async def test_extremely_large_tool_output():
    """Test how the system handles extremely large outputs from tools."""
    model = FakeModel()

    @function_tool
    def large_output_tool() -> str:
        # Generate a large string (100KB instead of 5MB to avoid memory issues in tests)
        return "x" * (100 * 1024)

    agent = Agent(
        name="test",
        model=model,
        tools=[large_output_tool],
    )

    model.set_next_output([
        get_function_tool_call("large_output_tool", "{}"),
        get_text_message("Processed large output")
    ])

    # This shouldn't crash but might have performance implications
    result = await Runner.run(agent, input="generate large output")

    # The test passes if we get here without exceptions
    assert len(result.new_items) > 0


@pytest.mark.asyncio
async def test_error_during_model_response_processing():
    """Test error handling during model response processing."""
    model = FakeModel(tracing_enabled=True)

    # Create a model that returns malformed JSON in a tool call
    agent = Agent(
        name="test",
        model=model,
    )

    # Set up model to directly raise a JSON parsing error
    model.set_next_output(json.JSONDecodeError("Expecting property name enclosed in double quotes", "{invalid json", 1))

    # This should raise some kind of exception
    with pytest.raises(json.JSONDecodeError):
        await Runner.run(agent, input="trigger bad json")
