from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, List, Optional

import pytest
from pydantic import BaseModel, Field

from agents import (
    Agent,
    FunctionTool,
    GuardrailFunctionOutput,
    InputGuardrail,
    ModelBehaviorError,
    OutputGuardrail,
    RunContextWrapper,
    Runner,
    function_tool,
)
from agents.exceptions import InputGuardrailTripwireTriggered, OutputGuardrailTripwireTriggered
from agents.tool import default_tool_error_function

from .fake_model import FakeModel
from .test_responses import (
    get_function_tool_call,
    get_malformed_function_tool_call,
    get_text_message,
    get_unknown_response_type,
)


@pytest.mark.asyncio
async def test_model_returning_unknown_tool():
    """Test behavior when the model attempts to call a tool that doesn't exist."""
    model = FakeModel()
    agent = Agent(
        name="test",
        model=model,
    )

    # Model tries to call a tool that doesn't exist
    model.set_next_output([
        get_function_tool_call("nonexistent_tool", "{}"),
        get_text_message("Fallback response")
    ])

    # This should raise some kind of exception
    with pytest.raises(ModelBehaviorError) as excinfo:
        await Runner.run(agent, input="call unknown tool")

    # The error should mention the nonexistent tool
    error_msg = str(excinfo.value).lower()
    assert "nonexistent_tool" in error_msg


@pytest.mark.asyncio
async def test_model_returning_malformed_schema():
    """Test behavior when the model returns a response that doesn't match any known schema."""
    model = FakeModel(tracing_enabled=True)
    agent = Agent(
        name="test",
        model=model,
    )

    # Set up model to directly raise a ModelBehaviorError
    model.set_next_output(ModelBehaviorError("Unexpected output type: unknown_type"))

    # This should raise an exception
    with pytest.raises(ModelBehaviorError):
        await Runner.run(agent, input="trigger malformed schema")


class ToolArgs(BaseModel):
    required_field: str = Field(...)
    integer_field: int = Field(...)
    nested_object: Dict[str, Any] = Field(...)


@pytest.mark.asyncio
async def test_model_returning_tool_response_json_schema_mismatch():
    """Test behavior when the model returns arguments that don't match the tool's schema."""
    model = FakeModel(tracing_enabled=True)

    @function_tool
    def complex_tool(args: ToolArgs) -> str:
        return f"Processed: {args.required_field}"

    agent = Agent(
        name="test",
        model=model,
        tools=[complex_tool],
    )

    # Set up model to directly raise a ModelBehaviorError for validation failure
    model.set_next_output(ModelBehaviorError("Validation error: required_field is a required property"))

    # This should raise some kind of validation error
    with pytest.raises(ModelBehaviorError) as excinfo:
        await Runner.run(agent, input="call complex tool incorrectly")

    # The error should be related to validation
    error_msg = str(excinfo.value).lower()
    assert "validation" in error_msg or "required" in error_msg or "type" in error_msg


@pytest.mark.asyncio
async def test_extremely_large_model_response():
    """Test handling of extremely large model responses."""
    model = FakeModel()

    # Generate a very large response (100KB)
    large_response = "x" * 100_000

    model.set_next_output([get_text_message(large_response)])

    agent = Agent(name="test", model=model)

    # This should not crash despite the large response
    result = await Runner.run(agent, input="generate large response")
    assert result.final_output == large_response


@pytest.mark.asyncio
async def test_unicode_and_special_characters():
    """Test handling of Unicode and special characters in model responses."""
    model = FakeModel()

    # Include various Unicode characters, emojis, and special characters
    unicode_response = "Unicode test: 你好世界 😊 🚀 ñáéíóú ⚠️ \u200b\t\n\r"

    model.set_next_output([get_text_message(unicode_response)])

    agent = Agent(name="test", model=model)

    # Verify Unicode is preserved
    result = await Runner.run(agent, input="respond with unicode")
    assert result.final_output == unicode_response


@pytest.mark.asyncio
async def test_malformed_json_in_function_call():
    """Test handling of malformed JSON in function calls."""
    model = FakeModel(tracing_enabled=True)

    @function_tool
    async def test_tool(param: str) -> str:
        return f"Tool received: {param}"

    agent = Agent(name="test", model=model, tools=[test_tool])

    # Set up model to directly raise a ModelBehaviorError for malformed JSON
    model.set_next_output(ModelBehaviorError("Failed to parse JSON: Expecting property name enclosed in double quotes"))

    # The agent should handle the malformed JSON gracefully
    with pytest.raises(ModelBehaviorError):
        await Runner.run(agent, input="call with bad json")


@pytest.mark.asyncio
async def test_input_validation_guardrail():
    """Test input validation guardrail rejecting problematic inputs."""
    model = FakeModel()
    model.set_next_output([get_text_message("This should not be reached")])

    async def input_validator(
        context: RunContextWrapper[Any], agent: Agent[Any], user_input: str
    ) -> GuardrailFunctionOutput:
        # Reject inputs containing certain patterns
        if re.search(r"(password|credit card|ssn)", user_input, re.IGNORECASE):
            return GuardrailFunctionOutput(
                output_info={"message": "Input contains sensitive information"},
                tripwire_triggered=True
            )
        return GuardrailFunctionOutput(
            output_info={"message": "Input is safe"},
            tripwire_triggered=False
        )

    agent = Agent(
        name="test",
        model=model,
        input_guardrails=[InputGuardrail(input_validator)],
    )

    # This should be rejected by the guardrail
    with pytest.raises(InputGuardrailTripwireTriggered):
        await Runner.run(agent, input="my password is 12345")


@pytest.mark.asyncio
async def test_output_validation_guardrail():
    """Test output validation guardrail rejecting problematic outputs."""
    model = FakeModel()
    model.set_next_output([get_text_message("This contains sensitive information like SSN 123-45-6789")])

    async def output_validator(
        context: RunContextWrapper[Any], agent: Agent[Any], agent_output: str
    ) -> GuardrailFunctionOutput:
        # Reject outputs containing certain patterns
        if re.search(r"\d{3}-\d{2}-\d{4}", agent_output):  # SSN pattern
            return GuardrailFunctionOutput(
                output_info={"message": "Output contains SSN"},
                tripwire_triggered=True
            )
        return GuardrailFunctionOutput(
            output_info={"message": "Output is safe"},
            tripwire_triggered=False
        )

    agent = Agent(
        name="test",
        model=model,
        output_guardrails=[OutputGuardrail(output_validator)],
    )

    # This should be rejected by the guardrail
    with pytest.raises(OutputGuardrailTripwireTriggered):
        await Runner.run(agent, input="tell me something")


@pytest.mark.asyncio
async def test_unicode_arguments_to_tools():
    """Test handling of Unicode arguments to tools."""
    model = FakeModel()

    @function_tool
    async def unicode_tool(text: str) -> str:
        # Simply echo back the Unicode text
        return f"Received: {text}"

    # Set up a model response with Unicode in the function call
    unicode_input = "Unicode test: 你好世界 😊 🚀 ñáéíóú"
    model.set_next_output([
        get_function_tool_call("unicode_tool", json.dumps({"text": unicode_input})),
        get_text_message("Tool handled Unicode")
    ])

    agent = Agent(name="test", model=model, tools=[unicode_tool])

    # Verify Unicode is preserved through tool calls
    result = await Runner.run(agent, input="use unicode")
    # Look for the tool output in the new_items
    tool_output_found = False
    for item in result.new_items:
        if hasattr(item, 'type') and item.type == 'tool_call_output_item':
            assert f"Received: {unicode_input}" in item.output
            tool_output_found = True
    assert tool_output_found, "Tool output item not found in result.new_items"
