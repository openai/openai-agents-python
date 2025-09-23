from __future__ import annotations

import asyncio
from typing import Any

import pytest
from openai.types.responses import ResponseFunctionToolCall

from agents import (
    Agent,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrail,
    ToolInputGuardrailData,
    ToolInputGuardrailTripwireTriggered,
    ToolOutputGuardrail,
    ToolOutputGuardrailData,
    ToolOutputGuardrailTripwireTriggered,
    UserError,
)
from agents.tool_context import ToolContext
from agents.tool_guardrails import tool_input_guardrail, tool_output_guardrail


def get_mock_tool_call(arguments: str = "{}") -> ResponseFunctionToolCall:
    """Helper to create a mock tool call for testing."""
    return ResponseFunctionToolCall(
        call_id="call_123", type="function_call", name="test_tool", arguments=arguments
    )


def get_mock_tool_context() -> ToolContext:
    """Helper to create a mock tool context for testing."""
    return ToolContext(context=None, tool_name="test_tool", tool_call_id="call_123")


def get_sync_input_guardrail(triggers: bool, output_info: Any | None = None):
    """Helper to create a sync input guardrail function."""

    def sync_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=triggers,
        )

    return sync_guardrail


def get_async_input_guardrail(triggers: bool, output_info: Any | None = None):
    """Helper to create an async input guardrail function."""

    async def async_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=triggers,
        )

    return async_guardrail


def get_sync_output_guardrail(triggers: bool, output_info: Any | None = None):
    """Helper to create a sync output guardrail function."""

    def sync_guardrail(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=triggers,
        )

    return sync_guardrail


def get_async_output_guardrail(triggers: bool, output_info: Any | None = None):
    """Helper to create an async output guardrail function."""

    async def async_guardrail(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=triggers,
        )

    return async_guardrail


@pytest.mark.asyncio
async def test_sync_tool_input_guardrail():
    """Test sync tool input guardrail execution."""
    # Test non-triggering guardrail
    guardrail: ToolInputGuardrail[Any] = ToolInputGuardrail(
        guardrail_function=get_sync_input_guardrail(triggers=False)
    )
    data = ToolInputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
        tool_call=get_mock_tool_call(),
    )
    result = await guardrail.run(data)
    assert not result.tripwire_triggered
    assert result.output_info is None

    # Test triggering guardrail
    guardrail_2: ToolInputGuardrail[Any] = ToolInputGuardrail(
        guardrail_function=get_sync_input_guardrail(triggers=True)
    )
    result = await guardrail_2.run(data)
    assert result.tripwire_triggered
    assert result.output_info is None

    # Test triggering guardrail with output info
    guardrail_3: ToolInputGuardrail[Any] = ToolInputGuardrail(
        guardrail_function=get_sync_input_guardrail(triggers=True, output_info="test_info")
    )
    result = await guardrail_3.run(data)
    assert result.tripwire_triggered
    assert result.output_info == "test_info"


@pytest.mark.asyncio
async def test_async_tool_input_guardrail():
    """Test async tool input guardrail execution."""
    # Test non-triggering guardrail
    guardrail: ToolInputGuardrail[Any] = ToolInputGuardrail(
        guardrail_function=get_async_input_guardrail(triggers=False)
    )
    data = ToolInputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
        tool_call=get_mock_tool_call(),
    )
    result = await guardrail.run(data)
    assert not result.tripwire_triggered
    assert result.output_info is None

    # Test triggering guardrail
    guardrail_2: ToolInputGuardrail[Any] = ToolInputGuardrail(
        guardrail_function=get_async_input_guardrail(triggers=True)
    )
    result = await guardrail_2.run(data)
    assert result.tripwire_triggered
    assert result.output_info is None

    # Test triggering guardrail with output info
    guardrail_3: ToolInputGuardrail[Any] = ToolInputGuardrail(
        guardrail_function=get_async_input_guardrail(triggers=True, output_info="test_info")
    )
    result = await guardrail_3.run(data)
    assert result.tripwire_triggered
    assert result.output_info == "test_info"


@pytest.mark.asyncio
async def test_sync_tool_output_guardrail():
    """Test sync tool output guardrail execution."""
    # Test non-triggering guardrail
    guardrail: ToolOutputGuardrail[Any] = ToolOutputGuardrail(
        guardrail_function=get_sync_output_guardrail(triggers=False)
    )
    data = ToolOutputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
        tool_call=get_mock_tool_call(),
        output="test output",
    )
    result = await guardrail.run(data)
    assert not result.tripwire_triggered
    assert result.output_info is None

    # Test triggering guardrail
    guardrail_2: ToolOutputGuardrail[Any] = ToolOutputGuardrail(
        guardrail_function=get_sync_output_guardrail(triggers=True)
    )
    result = await guardrail_2.run(data)
    assert result.tripwire_triggered
    assert result.output_info is None

    # Test triggering guardrail with output info
    guardrail_3: ToolOutputGuardrail[Any] = ToolOutputGuardrail(
        guardrail_function=get_sync_output_guardrail(triggers=True, output_info="test_info")
    )
    result = await guardrail_3.run(data)
    assert result.tripwire_triggered
    assert result.output_info == "test_info"


@pytest.mark.asyncio
async def test_async_tool_output_guardrail():
    """Test async tool output guardrail execution."""
    # Test non-triggering guardrail
    guardrail: ToolOutputGuardrail[Any] = ToolOutputGuardrail(
        guardrail_function=get_async_output_guardrail(triggers=False)
    )
    data = ToolOutputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
        tool_call=get_mock_tool_call(),
        output="test output",
    )
    result = await guardrail.run(data)
    assert not result.tripwire_triggered
    assert result.output_info is None

    # Test triggering guardrail
    guardrail_2: ToolOutputGuardrail[Any] = ToolOutputGuardrail(
        guardrail_function=get_async_output_guardrail(triggers=True)
    )
    result = await guardrail_2.run(data)
    assert result.tripwire_triggered
    assert result.output_info is None

    # Test triggering guardrail with output info
    guardrail_3: ToolOutputGuardrail[Any] = ToolOutputGuardrail(
        guardrail_function=get_async_output_guardrail(triggers=True, output_info="test_info")
    )
    result = await guardrail_3.run(data)
    assert result.tripwire_triggered
    assert result.output_info == "test_info"


@pytest.mark.asyncio
async def test_invalid_tool_input_guardrail_raises_user_error():
    """Test that invalid guardrail functions raise UserError."""
    with pytest.raises(UserError):
        # Purposely ignoring type error
        guardrail: ToolInputGuardrail[Any] = ToolInputGuardrail(guardrail_function="foo")  # type: ignore
        data = ToolInputGuardrailData(
            context=get_mock_tool_context(),
            agent=Agent(name="test"),
            tool_call=get_mock_tool_call(),
        )
        await guardrail.run(data)


@pytest.mark.asyncio
async def test_invalid_tool_output_guardrail_raises_user_error():
    """Test that invalid guardrail functions raise UserError."""
    with pytest.raises(UserError):
        # Purposely ignoring type error
        guardrail: ToolOutputGuardrail[Any] = ToolOutputGuardrail(guardrail_function="foo")  # type: ignore
        data = ToolOutputGuardrailData(
            context=get_mock_tool_context(),
            agent=Agent(name="test"),
            tool_call=get_mock_tool_call(),
            output="test output",
        )
        await guardrail.run(data)


# Test decorators


@tool_input_guardrail
def decorated_input_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    return ToolGuardrailFunctionOutput(
        output_info="test_1",
        tripwire_triggered=False,
    )


@tool_input_guardrail(name="Custom input name")
def decorated_named_input_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    return ToolGuardrailFunctionOutput(
        output_info="test_2",
        tripwire_triggered=False,
    )


@pytest.mark.asyncio
async def test_tool_input_guardrail_decorators():
    """Test input guardrail decorators."""
    data = ToolInputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
        tool_call=get_mock_tool_call(),
    )

    # Test basic decorator
    guardrail = decorated_input_guardrail
    result = await guardrail.run(data)
    assert not result.tripwire_triggered
    assert result.output_info == "test_1"

    # Test named decorator
    guardrail = decorated_named_input_guardrail
    result = await guardrail.run(data)
    assert not result.tripwire_triggered
    assert result.output_info == "test_2"
    assert guardrail.get_name() == "Custom input name"


@tool_output_guardrail
def decorated_output_guardrail(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
    return ToolGuardrailFunctionOutput(
        output_info="test_3",
        tripwire_triggered=False,
    )


@tool_output_guardrail(name="Custom output name")
def decorated_named_output_guardrail(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
    return ToolGuardrailFunctionOutput(
        output_info="test_4",
        tripwire_triggered=False,
    )


@pytest.mark.asyncio
async def test_tool_output_guardrail_decorators():
    """Test output guardrail decorators."""
    data = ToolOutputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
        tool_call=get_mock_tool_call(),
        output="test output",
    )

    # Test basic decorator
    guardrail = decorated_output_guardrail
    result = await guardrail.run(data)
    assert not result.tripwire_triggered
    assert result.output_info == "test_3"

    # Test named decorator
    guardrail = decorated_named_output_guardrail
    result = await guardrail.run(data)
    assert not result.tripwire_triggered
    assert result.output_info == "test_4"
    assert guardrail.get_name() == "Custom output name"


# Test practical examples


@pytest.mark.asyncio
async def test_password_blocking_input_guardrail():
    """Test a realistic input guardrail that blocks passwords."""

    @tool_input_guardrail
    def check_for_password(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        if "password" in data.tool_call.arguments.lower():
            return ToolGuardrailFunctionOutput(
                output_info={"blocked_word": "password"},
                tripwire_triggered=True,
                model_message="Tool call blocked: contains password",
            )
        return ToolGuardrailFunctionOutput(
            output_info="safe_input",
            tripwire_triggered=False,
        )

    # Test with password - should trigger
    data = ToolInputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
        tool_call=get_mock_tool_call('{"message": "Hello password world"}'),
    )
    result = await check_for_password.run(data)
    assert result.tripwire_triggered is True
    assert result.model_message == "Tool call blocked: contains password"
    assert result.output_info["blocked_word"] == "password"

    # Test without password - should pass
    data = ToolInputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
        tool_call=get_mock_tool_call('{"message": "Hello safe world"}'),
    )
    result = await check_for_password.run(data)
    assert result.tripwire_triggered is False
    assert result.output_info == "safe_input"


@pytest.mark.asyncio
async def test_ssn_blocking_output_guardrail():
    """Test a realistic output guardrail that blocks SSNs."""

    @tool_output_guardrail
    def check_for_ssn(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        output_str = str(data.output).lower()
        if "ssn" in output_str or "123-45-6789" in output_str:
            return ToolGuardrailFunctionOutput(
                output_info={"blocked_pattern": "SSN"},
                tripwire_triggered=True,
                model_message="Output blocked: contains SSN",
            )
        return ToolGuardrailFunctionOutput(
            output_info="safe_output",
            tripwire_triggered=False,
        )

    # Test with SSN in output - should trigger
    data = ToolOutputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
        tool_call=get_mock_tool_call(),
        output="User SSN is 123-45-6789",
    )
    result = await check_for_ssn.run(data)
    assert result.tripwire_triggered is True
    assert result.model_message == "Output blocked: contains SSN"
    assert result.output_info["blocked_pattern"] == "SSN"

    # Test with safe output - should pass
    data = ToolOutputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
        tool_call=get_mock_tool_call(),
        output="User name is John Doe",
    )
    result = await check_for_ssn.run(data)
    assert result.tripwire_triggered is False
    assert result.output_info == "safe_output"


def test_tool_input_guardrail_exception():
    """Test the tool input guardrail tripwire exception."""

    @tool_input_guardrail
    def test_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput(
            output_info="test",
            tripwire_triggered=True,
            model_message="blocked",
        )

    output = ToolGuardrailFunctionOutput(
        output_info="test",
        tripwire_triggered=True,
        model_message="blocked",
    )

    exception = ToolInputGuardrailTripwireTriggered(
        guardrail=test_guardrail,
        output=output,
    )

    assert exception.guardrail == test_guardrail
    assert exception.output == output
    assert "ToolInputGuardrail" in str(exception)


def test_tool_output_guardrail_exception():
    """Test the tool output guardrail tripwire exception."""

    @tool_output_guardrail
    def test_guardrail(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput(
            output_info="test",
            tripwire_triggered=True,
            model_message="blocked",
        )

    output = ToolGuardrailFunctionOutput(
        output_info="test",
        tripwire_triggered=True,
        model_message="blocked",
    )

    exception = ToolOutputGuardrailTripwireTriggered(
        guardrail=test_guardrail,
        output=output,
    )

    assert exception.guardrail == test_guardrail
    assert exception.output == output
    assert "ToolOutputGuardrail" in str(exception)


if __name__ == "__main__":
    # Run a simple test to verify functionality
    async def main():
        print("Testing tool guardrails...")

        @tool_input_guardrail
        def test_guard(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
            return ToolGuardrailFunctionOutput(
                output_info="test_passed",
                tripwire_triggered=False,
            )

        print(f"✅ Created guardrail: {test_guard.get_name()}")
        print("✅ All basic tests passed!")

    asyncio.run(main())
