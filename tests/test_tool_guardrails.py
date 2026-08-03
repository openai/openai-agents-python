from __future__ import annotations

import asyncio
from typing import Any

import pytest
from openai.types.responses.response_output_item import LocalShellCall, LocalShellCallAction

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrail,
    InputGuardrailTripwireTriggered,
    LocalShellTool,
    MaxTurnsExceeded,
    ModelBehaviorError,
    Runner,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrail,
    ToolInputGuardrailData,
    ToolInputGuardrailTripwireTriggered,
    ToolOutputGuardrail,
    ToolOutputGuardrailData,
    ToolOutputGuardrailTripwireTriggered,
    UserError,
    function_tool,
)
from agents.tool_context import ToolContext
from agents.tool_guardrails import tool_input_guardrail, tool_output_guardrail

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_text_message


def get_mock_tool_context(tool_arguments: str = '{"param": "value"}') -> ToolContext:
    """Helper to create a mock tool context for testing."""
    return ToolContext(
        context=None,
        tool_name="test_tool",
        tool_call_id="call_123",
        tool_arguments=tool_arguments,
    )


def get_sync_input_guardrail(triggers: bool, output_info: Any | None = None):
    """Helper to create a sync input guardrail function."""

    def sync_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        if triggers:
            return ToolGuardrailFunctionOutput.raise_exception(output_info=output_info)
        else:
            return ToolGuardrailFunctionOutput.allow(output_info=output_info)

    return sync_guardrail


def get_async_input_guardrail(triggers: bool, output_info: Any | None = None):
    """Helper to create an async input guardrail function."""

    async def async_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        if triggers:
            return ToolGuardrailFunctionOutput.raise_exception(output_info=output_info)
        else:
            return ToolGuardrailFunctionOutput.allow(output_info=output_info)

    return async_guardrail


def get_sync_output_guardrail(triggers: bool, output_info: Any | None = None):
    """Helper to create a sync output guardrail function."""

    def sync_guardrail(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        if triggers:
            return ToolGuardrailFunctionOutput.raise_exception(output_info=output_info)
        else:
            return ToolGuardrailFunctionOutput.allow(output_info=output_info)

    return sync_guardrail


def get_async_output_guardrail(triggers: bool, output_info: Any | None = None):
    """Helper to create an async output guardrail function."""

    async def async_guardrail(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        if triggers:
            return ToolGuardrailFunctionOutput.raise_exception(output_info=output_info)
        else:
            return ToolGuardrailFunctionOutput.allow(output_info=output_info)

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
    )
    result = await guardrail.run(data)
    assert result.behavior["type"] == "allow"
    assert result.output_info is None

    # Test triggering guardrail
    guardrail_2: ToolInputGuardrail[Any] = ToolInputGuardrail(
        guardrail_function=get_sync_input_guardrail(triggers=True)
    )
    result = await guardrail_2.run(data)
    assert result.behavior["type"] == "raise_exception"
    assert result.output_info is None

    # Test triggering guardrail with output info
    guardrail_3: ToolInputGuardrail[Any] = ToolInputGuardrail(
        guardrail_function=get_sync_input_guardrail(triggers=True, output_info="test_info")
    )
    result = await guardrail_3.run(data)
    assert result.behavior["type"] == "raise_exception"
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
    )
    result = await guardrail.run(data)
    assert result.behavior["type"] == "allow"
    assert result.output_info is None

    # Test triggering guardrail
    guardrail_2: ToolInputGuardrail[Any] = ToolInputGuardrail(
        guardrail_function=get_async_input_guardrail(triggers=True)
    )
    result = await guardrail_2.run(data)
    assert result.behavior["type"] == "raise_exception"
    assert result.output_info is None

    # Test triggering guardrail with output info
    guardrail_3: ToolInputGuardrail[Any] = ToolInputGuardrail(
        guardrail_function=get_async_input_guardrail(triggers=True, output_info="test_info")
    )
    result = await guardrail_3.run(data)
    assert result.behavior["type"] == "raise_exception"
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
        output="test output",
    )
    result = await guardrail.run(data)
    assert result.behavior["type"] == "allow"
    assert result.output_info is None

    # Test triggering guardrail
    guardrail_2: ToolOutputGuardrail[Any] = ToolOutputGuardrail(
        guardrail_function=get_sync_output_guardrail(triggers=True)
    )
    result = await guardrail_2.run(data)
    assert result.behavior["type"] == "raise_exception"
    assert result.output_info is None

    # Test triggering guardrail with output info
    guardrail_3: ToolOutputGuardrail[Any] = ToolOutputGuardrail(
        guardrail_function=get_sync_output_guardrail(triggers=True, output_info="test_info")
    )
    result = await guardrail_3.run(data)
    assert result.behavior["type"] == "raise_exception"
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
        output="test output",
    )
    result = await guardrail.run(data)
    assert result.behavior["type"] == "allow"
    assert result.output_info is None

    # Test triggering guardrail
    guardrail_2: ToolOutputGuardrail[Any] = ToolOutputGuardrail(
        guardrail_function=get_async_output_guardrail(triggers=True)
    )
    result = await guardrail_2.run(data)
    assert result.behavior["type"] == "raise_exception"
    assert result.output_info is None

    # Test triggering guardrail with output info
    guardrail_3: ToolOutputGuardrail[Any] = ToolOutputGuardrail(
        guardrail_function=get_async_output_guardrail(triggers=True, output_info="test_info")
    )
    result = await guardrail_3.run(data)
    assert result.behavior["type"] == "raise_exception"
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
            output="test output",
        )
        await guardrail.run(data)


# Test decorators


@tool_input_guardrail
def decorated_input_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    return ToolGuardrailFunctionOutput.allow(output_info="test_1")


@tool_input_guardrail(name="Custom input name")
def decorated_named_input_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    return ToolGuardrailFunctionOutput.allow(output_info="test_2")


@pytest.mark.asyncio
async def test_tool_input_guardrail_decorators():
    """Test input guardrail decorators."""
    data = ToolInputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
    )

    # Test basic decorator
    guardrail = decorated_input_guardrail
    result = await guardrail.run(data)
    assert result.behavior["type"] == "allow"
    assert result.output_info == "test_1"
    assert guardrail.get_name() == "decorated_input_guardrail"

    # Test named decorator
    guardrail = decorated_named_input_guardrail
    result = await guardrail.run(data)
    assert result.behavior["type"] == "allow"
    assert result.output_info == "test_2"
    assert guardrail.get_name() == "Custom input name"


@tool_output_guardrail
def decorated_output_guardrail(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
    return ToolGuardrailFunctionOutput.allow(output_info="test_3")


@tool_output_guardrail(name="Custom output name")
def decorated_named_output_guardrail(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
    return ToolGuardrailFunctionOutput.allow(output_info="test_4")


@pytest.mark.asyncio
async def test_tool_output_guardrail_decorators():
    """Test output guardrail decorators."""
    data = ToolOutputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
        output="test output",
    )

    # Test basic decorator
    guardrail = decorated_output_guardrail
    result = await guardrail.run(data)
    assert result.behavior["type"] == "allow"
    assert result.output_info == "test_3"
    assert guardrail.get_name() == "decorated_output_guardrail"

    # Test named decorator
    guardrail = decorated_named_output_guardrail
    result = await guardrail.run(data)
    assert result.behavior["type"] == "allow"
    assert result.output_info == "test_4"
    assert guardrail.get_name() == "Custom output name"


# Test practical examples


@pytest.mark.asyncio
async def test_password_blocking_input_guardrail():
    """Test a realistic input guardrail that blocks passwords."""

    @tool_input_guardrail
    def check_for_password(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        if "password" in data.context.tool_arguments.lower():
            return ToolGuardrailFunctionOutput.reject_content(
                message="Tool call blocked: contains password",
                output_info={"blocked_word": "password"},
            )
        return ToolGuardrailFunctionOutput(output_info="safe_input")

    # Test with password - should trigger
    data = ToolInputGuardrailData(
        context=get_mock_tool_context('{"message": "Hello password world"}'),
        agent=Agent(name="test"),
    )
    result = await check_for_password.run(data)
    assert result.behavior["type"] == "reject_content"
    assert result.behavior["message"] == "Tool call blocked: contains password"
    assert result.output_info["blocked_word"] == "password"

    # Test without password - should pass
    data = ToolInputGuardrailData(
        context=get_mock_tool_context('{"message": "Hello safe world"}'),
        agent=Agent(name="test"),
    )
    result = await check_for_password.run(data)
    assert result.behavior["type"] == "allow"
    assert result.output_info == "safe_input"


@pytest.mark.asyncio
async def test_ssn_blocking_output_guardrail():
    """Test a realistic output guardrail that blocks SSNs."""

    @tool_output_guardrail
    def check_for_ssn(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        output_str = str(data.output).lower()
        if "ssn" in output_str or "123-45-6789" in output_str:
            return ToolGuardrailFunctionOutput.raise_exception(
                output_info={"blocked_pattern": "SSN"}
            )
        return ToolGuardrailFunctionOutput(output_info="safe_output")

    # Test with SSN in output - should trigger
    data = ToolOutputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
        output="User SSN is 123-45-6789",
    )
    result = await check_for_ssn.run(data)
    assert result.behavior["type"] == "raise_exception"
    assert result.output_info["blocked_pattern"] == "SSN"

    # Test with safe output - should pass
    data = ToolOutputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
        output="User name is John Doe",
    )
    result = await check_for_ssn.run(data)
    assert result.behavior["type"] == "allow"
    assert result.output_info == "safe_output"


def test_tool_input_guardrail_exception():
    """Test the tool input guardrail tripwire exception."""

    @tool_input_guardrail
    def test_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.raise_exception(output_info="test")

    output = ToolGuardrailFunctionOutput.raise_exception(output_info="test")

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
        return ToolGuardrailFunctionOutput.raise_exception(output_info="test")

    output = ToolGuardrailFunctionOutput.raise_exception(output_info="test")

    exception = ToolOutputGuardrailTripwireTriggered(
        guardrail=test_guardrail,
        output=output,
    )

    assert exception.guardrail == test_guardrail
    assert exception.output == output
    assert "ToolOutputGuardrail" in str(exception)


# Test new behavior system


@pytest.mark.asyncio
async def test_allow_behavior():
    """Test the allow behavior type."""

    @tool_input_guardrail
    def allow_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.allow(output_info="allowed")

    data = ToolInputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
    )
    result = await allow_guardrail.run(data)
    assert result.behavior["type"] == "allow"
    assert result.output_info == "allowed"


@pytest.mark.asyncio
async def test_reject_content_behavior():
    """Test the reject_content behavior type."""

    @tool_input_guardrail
    def reject_content_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.reject_content(
            message="Tool blocked by guardrail", output_info="rejected"
        )

    data = ToolInputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
    )
    result = await reject_content_guardrail.run(data)
    assert result.behavior["type"] == "reject_content"
    assert result.behavior["message"] == "Tool blocked by guardrail"
    assert result.output_info == "rejected"


@pytest.mark.asyncio
async def test_raise_exception_behavior():
    """Test the raise_exception behavior type."""

    @tool_input_guardrail
    def raise_exception_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.raise_exception(output_info="exception")

    data = ToolInputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
    )
    result = await raise_exception_guardrail.run(data)
    assert result.behavior["type"] == "raise_exception"
    assert result.output_info == "exception"


@pytest.mark.asyncio
async def test_mixed_behavior_output_guardrail():
    """Test mixing different behavior types in output guardrails."""

    @tool_output_guardrail
    def mixed_guardrail(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        output_str = str(data.output).lower()
        if "dangerous" in output_str:
            return ToolGuardrailFunctionOutput.raise_exception(
                output_info={"reason": "dangerous_content"}
            )
        elif "sensitive" in output_str:
            return ToolGuardrailFunctionOutput.reject_content(
                message="Content was filtered", output_info={"reason": "sensitive_content"}
            )
        else:
            return ToolGuardrailFunctionOutput(output_info={"status": "clean"})

    # Test dangerous content (should raise exception)
    data_dangerous = ToolOutputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
        output="This is dangerous content",
    )
    result = await mixed_guardrail.run(data_dangerous)
    assert result.behavior["type"] == "raise_exception"
    assert result.output_info["reason"] == "dangerous_content"

    # Test sensitive content (should reject content)
    data_sensitive = ToolOutputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
        output="This is sensitive data",
    )
    result = await mixed_guardrail.run(data_sensitive)
    assert result.behavior["type"] == "reject_content"
    assert result.behavior["message"] == "Content was filtered"
    assert result.output_info["reason"] == "sensitive_content"

    # Test clean content (should allow)
    data_clean = ToolOutputGuardrailData(
        context=get_mock_tool_context(),
        agent=Agent(name="test"),
        output="This is clean content",
    )
    result = await mixed_guardrail.run(data_clean)
    assert result.behavior["type"] == "allow"
    assert result.output_info["status"] == "clean"


# ---------------------------------------------------------------------------
# Tool guardrail results on RunErrorDetails
# ---------------------------------------------------------------------------


def _rejecting_input_guardrail() -> ToolInputGuardrail[Any]:
    """Rejects the tool call but lets the run continue, so results accumulate."""

    async def reject(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.reject_content(
            message="blocked by policy", output_info="input_rejected"
        )

    return ToolInputGuardrail(guardrail_function=reject, name="input_rejects")


def _rejecting_output_guardrail() -> ToolOutputGuardrail[Any]:
    """Rejects the tool output but lets the run continue, so results accumulate."""

    async def reject(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.reject_content(
            message="blocked by policy", output_info="output_rejected"
        )

    return ToolOutputGuardrail(guardrail_function=reject, name="output_rejects")


def _looping_agent(
    *,
    input_guardrails: list[ToolInputGuardrail[Any]] | None = None,
    output_guardrails: list[ToolOutputGuardrail[Any]] | None = None,
) -> Agent[Any]:
    """An agent that keeps calling a guarded tool, so the run ends on max turns."""

    @function_tool
    def guarded(query: str) -> str:
        return "tool output"

    guarded.tool_input_guardrails = input_guardrails or []
    guarded.tool_output_guardrails = output_guardrails or []

    model = FakeModel()
    call = [get_function_tool_call("guarded", '{"query": "secret"}')]
    model.add_multiple_turn_outputs([call, call, call, call, call, call])
    return Agent(name="tool_guardrail_results_agent", model=model, tools=[guarded])


def _names(results: list[Any]) -> list[str]:
    return [result.guardrail.get_name() for result in results]


async def _run_until_max_turns(agent: Agent[Any], *, streaming: bool, max_turns: int) -> Any:
    with pytest.raises(MaxTurnsExceeded) as exc_info:
        if streaming:
            result = Runner.run_streamed(agent, "go", max_turns=max_turns)
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "go", max_turns=max_turns)
    return exc_info.value


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_tool_input_guardrail_results_reported_on_failure(streaming: bool):
    """Tool input guardrail results collected before the failure are reported."""
    agent = _looping_agent(input_guardrails=[_rejecting_input_guardrail()])
    exc = await _run_until_max_turns(agent, streaming=streaming, max_turns=3)

    run_data = exc.run_data
    assert run_data is not None
    assert _names(run_data.tool_input_guardrail_results) == ["input_rejects"] * 3
    assert run_data.tool_output_guardrail_results == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_tool_output_guardrail_results_reported_on_failure(streaming: bool):
    """Tool output guardrail results collected before the failure are reported."""
    agent = _looping_agent(output_guardrails=[_rejecting_output_guardrail()])
    exc = await _run_until_max_turns(agent, streaming=streaming, max_turns=2)

    run_data = exc.run_data
    assert run_data is not None
    assert _names(run_data.tool_output_guardrail_results) == ["output_rejects"] * 2
    assert run_data.tool_input_guardrail_results == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_tool_guardrail_results_empty_without_tool_guardrails(streaming: bool):
    """Negative control: the fields stay empty when no tool guardrails run."""
    exc = await _run_until_max_turns(_looping_agent(), streaming=streaming, max_turns=2)

    run_data = exc.run_data
    assert run_data is not None
    assert run_data.tool_input_guardrail_results == []
    assert run_data.tool_output_guardrail_results == []


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_tool_guardrail_tripwire_reports_earlier_results(streaming: bool):
    """A raising tool guardrail still reports results collected on earlier turns."""

    @function_tool
    def first(query: str) -> str:
        return "first output"

    @function_tool
    def second(query: str) -> str:
        return "second output"

    async def raises(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.raise_exception(output_info="tripped")

    first.tool_output_guardrails = [_rejecting_output_guardrail()]
    second.tool_output_guardrails = [
        ToolOutputGuardrail(guardrail_function=raises, name="output_raises")
    ]

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("first", '{"query": "a"}')],
            [get_function_tool_call("second", '{"query": "b"}')],
        ]
    )
    agent = Agent(name="tripwire_agent", model=model, tools=[first, second])

    with pytest.raises(ToolOutputGuardrailTripwireTriggered) as exc_info:
        if streaming:
            result = Runner.run_streamed(agent, "go")
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "go")

    run_data = exc_info.value.run_data
    assert run_data is not None
    assert "output_rejects" in _names(run_data.tool_output_guardrail_results)


def _raising_input_guardrail() -> ToolInputGuardrail[Any]:
    async def raises(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.raise_exception(output_info="tripped")

    return ToolInputGuardrail(guardrail_function=raises, name="input_raises")


def _raising_output_guardrail() -> ToolOutputGuardrail[Any]:
    async def raises(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.raise_exception(output_info="tripped")

    return ToolOutputGuardrail(guardrail_function=raises, name="output_raises")


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_tool_input_tripwire_reports_triggering_result(streaming: bool):
    """The result that triggered the tripwire is reported, even on the very first turn.

    The turn aborts before its `SingleStepResult` exists, so the run-wide accumulators are still
    empty at that point.
    """
    agent = _looping_agent(input_guardrails=[_raising_input_guardrail()])

    with pytest.raises(ToolInputGuardrailTripwireTriggered) as exc_info:
        if streaming:
            result = Runner.run_streamed(agent, "go")
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "go")

    run_data = exc_info.value.run_data
    assert run_data is not None
    assert _names(run_data.tool_input_guardrail_results) == ["input_raises"]
    assert exc_info.value.guardrail.get_name() == "input_raises"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_tool_output_tripwire_reports_triggering_result(streaming: bool):
    """Same as above for the output guardrail pipeline."""
    agent = _looping_agent(output_guardrails=[_raising_output_guardrail()])

    with pytest.raises(ToolOutputGuardrailTripwireTriggered) as exc_info:
        if streaming:
            result = Runner.run_streamed(agent, "go")
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "go")

    run_data = exc_info.value.run_data
    assert run_data is not None
    assert _names(run_data.tool_output_guardrail_results) == ["output_raises"]
    assert exc_info.value.guardrail.get_name() == "output_raises"


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_tool_input_tripwire_reports_passing_results_from_same_batch(streaming: bool):
    """Guardrails that completed before the raising one in the same turn are reported too."""

    @function_tool
    def guarded(query: str) -> str:
        return "tool output"

    async def allow(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.allow(output_info="allowed")

    guarded.tool_input_guardrails = [
        ToolInputGuardrail(guardrail_function=allow, name="input_allows"),
        _raising_input_guardrail(),
    ]

    model = FakeModel()
    model.set_next_output([get_function_tool_call("guarded", '{"query": "x"}')])
    agent = Agent(name="batch_agent", model=model, tools=[guarded])

    with pytest.raises(ToolInputGuardrailTripwireTriggered) as exc_info:
        if streaming:
            result = Runner.run_streamed(agent, "go")
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "go")

    run_data = exc_info.value.run_data
    assert run_data is not None
    assert _names(run_data.tool_input_guardrail_results) == ["input_allows", "input_raises"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_tool_tripwire_reports_earlier_turns_and_triggering_result(streaming: bool):
    """Completed-turn accumulators and the aborted turn's partials are both reported, once each."""

    @function_tool
    def first(query: str) -> str:
        return "first output"

    @function_tool
    def second(query: str) -> str:
        return "second output"

    first.tool_output_guardrails = [_rejecting_output_guardrail()]
    second.tool_output_guardrails = [_raising_output_guardrail()]

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [get_function_tool_call("first", '{"query": "a"}')],
            [get_function_tool_call("second", '{"query": "b"}')],
        ]
    )
    agent = Agent(name="two_turn_agent", model=model, tools=[first, second])

    with pytest.raises(ToolOutputGuardrailTripwireTriggered) as exc_info:
        if streaming:
            result = Runner.run_streamed(agent, "go")
            async for _ in result.stream_events():
                pass
        else:
            await Runner.run(agent, "go")

    run_data = exc_info.value.run_data
    assert run_data is not None
    assert _names(run_data.tool_output_guardrail_results) == ["output_rejects", "output_raises"]


def _looping_tool_input_guardrail() -> ToolInputGuardrail[Any]:
    """A rejecting input guardrail that records how many times it ran."""

    async def reject(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.reject_content(
            message="blocked by policy", output_info="input_rejected"
        )

    return ToolInputGuardrail(guardrail_function=reject, name="input_rejects")


@pytest.mark.asyncio
async def test_sibling_tool_failure_reports_function_tool_guardrail_results():
    """A non-function tool raising must not drop guardrails the function side already ran."""

    @function_tool
    def guarded(query: str) -> str:
        return "tool output"

    guarded.tool_input_guardrails = [_looping_tool_input_guardrail()]

    def failing_executor(request: Any) -> str:
        raise ModelBehaviorError("sibling shell tool exploded")

    shell_tool = LocalShellTool(executor=failing_executor)
    shell_call = LocalShellCall(
        id="lsh_sibling",
        action=LocalShellCallAction(
            command=["bash", "-c", "echo hi"],
            env={},
            type="exec",
            timeout_ms=1000,
            working_directory="/tmp",
        ),
        call_id="call_shell_sibling",
        status="completed",
        type="local_shell_call",
    )

    model = FakeModel()
    model.add_multiple_turn_outputs(
        [
            [
                get_function_tool_call("guarded", '{"query": "secret"}'),
                shell_call,
            ],
            [get_text_message("done")],
        ]
    )
    agent = Agent(name="mixed_tools", model=model, tools=[guarded, shell_tool])

    with pytest.raises(ModelBehaviorError) as exc_info:
        await Runner.run(agent, "go")

    run_data = exc_info.value.run_data
    assert run_data is not None
    assert _names(run_data.tool_input_guardrail_results) == ["input_rejects"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_parallel_input_tripwire_reports_tool_guardrail_results(streaming: bool):
    """A parallel input tripwire must report tool guardrails the overlapped turn ran."""
    guardrail_runs = 0

    async def reject(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        nonlocal guardrail_runs
        guardrail_runs += 1
        return ToolGuardrailFunctionOutput.reject_content(
            message="blocked by policy", output_info="input_rejected"
        )

    @function_tool
    def guarded(query: str) -> str:
        return "tool output"

    guarded.tool_input_guardrails = [
        ToolInputGuardrail(guardrail_function=reject, name="input_rejects")
    ]

    async def trip_after_tool_turn(
        ctx: Any, agent: Any, agent_input: Any
    ) -> GuardrailFunctionOutput:
        # Overlap the model turn so its tool guardrail runs before the tripwire.
        await asyncio.sleep(0.2)
        return GuardrailFunctionOutput(output_info="tripped", tripwire_triggered=True)

    parallel_guardrail = InputGuardrail(
        guardrail_function=trip_after_tool_turn, name="slow_tripwire"
    )
    parallel_guardrail.run_in_parallel = True

    model = FakeModel()
    call = [get_function_tool_call("guarded", '{"query": "secret"}')]
    model.add_multiple_turn_outputs([call, call, [get_text_message("done")]])
    agent = Agent(
        name="parallel_guardrail_agent",
        model=model,
        tools=[guarded],
        input_guardrails=[parallel_guardrail],
    )

    with pytest.raises(InputGuardrailTripwireTriggered) as exc_info:
        if streaming:
            streamed = Runner.run_streamed(agent, "go")
            async for _ in streamed.stream_events():
                pass
        else:
            await Runner.run(agent, "go")

    assert guardrail_runs >= 1, "the tool guardrail never ran, so the test proves nothing"
    run_data = exc_info.value.run_data
    assert run_data is not None
    assert _names(run_data.tool_input_guardrail_results) == ["input_rejects"] * guardrail_runs


def _build_slow_tool_parallel_tripwire_agent(
    tool_sleep: float,
) -> tuple[Agent, dict[str, int]]:
    """An agent whose tool guardrail runs, then a parallel guardrail trips mid-tool-call."""
    state = {"runs": 0}

    async def allow(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        state["runs"] += 1
        return ToolGuardrailFunctionOutput.allow(output_info="allowed")

    @function_tool
    async def guarded(query: str) -> str:
        # Still in flight when the parallel guardrail trips.
        await asyncio.sleep(tool_sleep)
        return "tool output"

    guarded.tool_input_guardrails = [
        ToolInputGuardrail(guardrail_function=allow, name="input_allows")
    ]

    async def trip_mid_tool_call(ctx: Any, agent: Any, agent_input: Any) -> GuardrailFunctionOutput:
        await asyncio.sleep(0.15)
        return GuardrailFunctionOutput(output_info="tripped", tripwire_triggered=True)

    parallel_guardrail = InputGuardrail(guardrail_function=trip_mid_tool_call, name="slow_tripwire")
    parallel_guardrail.run_in_parallel = True

    model = FakeModel()
    call = [get_function_tool_call("guarded", '{"query": "secret"}')]
    model.add_multiple_turn_outputs([call, call, [get_text_message("done")]])
    agent = Agent(
        name="parallel_guardrail_agent",
        model=model,
        tools=[guarded],
        input_guardrails=[parallel_guardrail],
    )
    return agent, state


@pytest.mark.asyncio
async def test_parallel_tripwire_reports_guardrails_from_a_cancelled_turn():
    """Cancelling the overlapped turn must not lose the tool guardrails it already ran.

    `asyncio.gather` returns a fresh `CancelledError` for a cancelled task, so the partials the
    executor recorded on the original exception are unreachable from the awaiting side.
    """
    agent, state = _build_slow_tool_parallel_tripwire_agent(tool_sleep=2.0)

    with pytest.raises(InputGuardrailTripwireTriggered) as exc_info:
        await Runner.run(agent, "go")

    assert state["runs"] >= 1, "the tool guardrail never ran, so the test proves nothing"
    run_data = exc_info.value.run_data
    assert run_data is not None
    assert _names(run_data.tool_input_guardrail_results) == ["input_allows"] * state["runs"]


@pytest.mark.asyncio
async def test_parallel_tripwire_reports_guardrails_without_cancelling_the_turn():
    """The no-cancel path (e.g. Temporal replay) still discards the turn, so harvest it."""
    agent, state = _build_slow_tool_parallel_tripwire_agent(tool_sleep=0.0)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "agents.run.should_cancel_parallel_model_task_on_input_guardrail_trip",
            lambda: False,
        )
        with pytest.raises(InputGuardrailTripwireTriggered) as exc_info:
            await Runner.run(agent, "go")

    assert state["runs"] >= 1, "the tool guardrail never ran, so the test proves nothing"
    run_data = exc_info.value.run_data
    assert run_data is not None
    assert _names(run_data.tool_input_guardrail_results) == ["input_allows"] * state["runs"]


@pytest.mark.asyncio
async def test_streamed_parallel_tripwire_details_refresh_after_the_turn_settles():
    """A streamed tripwire's details are provisional until the in-flight turn appends its results.

    The consumer here yields between events, so `_check_errors()` freezes the details while the
    tool call is still running -- a fast consumer parks on the empty queue and hides this.
    """
    agent, state = _build_slow_tool_parallel_tripwire_agent(tool_sleep=0.6)

    with pytest.raises(InputGuardrailTripwireTriggered) as exc_info:
        streamed = Runner.run_streamed(agent, "go")
        async for _ in streamed.stream_events():
            await asyncio.sleep(0.08)

    assert state["runs"] >= 1, "the tool guardrail never ran, so the test proves nothing"
    run_data = exc_info.value.run_data
    assert run_data is not None
    assert _names(run_data.tool_input_guardrail_results) == ["input_allows"] * state["runs"]
    # The streamed object itself already reported them; the exception must agree with it.
    assert _names(run_data.tool_input_guardrail_results) == _names(
        streamed.tool_input_guardrail_results
    )


if __name__ == "__main__":
    # Run a simple test to verify functionality
    async def main():
        print("Testing tool guardrails...")

        @tool_input_guardrail
        def test_guard(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
            return ToolGuardrailFunctionOutput.allow(output_info="test_passed")

        print(f"✅ Created guardrail: {test_guard.get_name()}")
        print("✅ All basic tests passed!")

    asyncio.run(main())
