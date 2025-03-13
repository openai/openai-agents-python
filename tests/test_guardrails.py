from __future__ import annotations

import json
import re
import os
from typing import Any, Dict, List, Optional

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrail,
    OutputGuardrail,
    RunContextWrapper,
    Runner,
    TResponseInputItem,
    UserError,
    function_tool,
)
from agents.guardrail import input_guardrail, output_guardrail
from agents.exceptions import InputGuardrailTripwireTriggered, OutputGuardrailTripwireTriggered

from .fake_model import FakeModel
from .test_responses import (
    get_function_tool_call,
    get_text_message,
)


def get_sync_guardrail(triggers: bool, output_info: Any | None = None):
    def sync_guardrail(
        context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ):
        return GuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=triggers,
        )

    return sync_guardrail


@pytest.mark.asyncio
async def test_sync_input_guardrail():
    guardrail = InputGuardrail(guardrail_function=get_sync_guardrail(triggers=False))
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = InputGuardrail(guardrail_function=get_sync_guardrail(triggers=True))
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = InputGuardrail(
        guardrail_function=get_sync_guardrail(triggers=True, output_info="test")
    )
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info == "test"


def get_async_input_guardrail(triggers: bool, output_info: Any | None = None):
    async def async_guardrail(
        context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ):
        return GuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=triggers,
        )

    return async_guardrail


@pytest.mark.asyncio
async def test_async_input_guardrail():
    guardrail = InputGuardrail(guardrail_function=get_async_input_guardrail(triggers=False))
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = InputGuardrail(guardrail_function=get_async_input_guardrail(triggers=True))
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = InputGuardrail(
        guardrail_function=get_async_input_guardrail(triggers=True, output_info="test")
    )
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info == "test"


@pytest.mark.asyncio
async def test_invalid_input_guardrail_raises_user_error():
    with pytest.raises(UserError):
        # Purposely ignoring type error
        guardrail = InputGuardrail(guardrail_function="foo")  # type: ignore
        await guardrail.run(
            agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
        )


def get_sync_output_guardrail(triggers: bool, output_info: Any | None = None):
    def sync_guardrail(context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any):
        return GuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=triggers,
        )

    return sync_guardrail


@pytest.mark.asyncio
async def test_sync_output_guardrail():
    guardrail = OutputGuardrail(guardrail_function=get_sync_output_guardrail(triggers=False))
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = OutputGuardrail(guardrail_function=get_sync_output_guardrail(triggers=True))
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = OutputGuardrail(
        guardrail_function=get_sync_output_guardrail(triggers=True, output_info="test")
    )
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info == "test"


def get_async_output_guardrail(triggers: bool, output_info: Any | None = None):
    async def async_guardrail(
        context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
    ):
        return GuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=triggers,
        )

    return async_guardrail


@pytest.mark.asyncio
async def test_async_output_guardrail():
    guardrail = OutputGuardrail(guardrail_function=get_async_output_guardrail(triggers=False))
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = OutputGuardrail(guardrail_function=get_async_output_guardrail(triggers=True))
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = OutputGuardrail(
        guardrail_function=get_async_output_guardrail(triggers=True, output_info="test")
    )
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info == "test"


@pytest.mark.asyncio
async def test_invalid_output_guardrail_raises_user_error():
    with pytest.raises(UserError):
        # Purposely ignoring type error
        guardrail = OutputGuardrail(guardrail_function="foo")  # type: ignore
        await guardrail.run(
            agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
        )


@input_guardrail
def decorated_input_guardrail(
    context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(
        output_info="test_1",
        tripwire_triggered=False,
    )


@input_guardrail(name="Custom name")
def decorated_named_input_guardrail(
    context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(
        output_info="test_2",
        tripwire_triggered=False,
    )


@pytest.mark.asyncio
async def test_input_guardrail_decorators():
    guardrail = decorated_input_guardrail
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info == "test_1"

    guardrail = decorated_named_input_guardrail
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info == "test_2"
    assert guardrail.get_name() == "Custom name"


@output_guardrail
def decorated_output_guardrail(
    context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(
        output_info="test_3",
        tripwire_triggered=False,
    )


@output_guardrail(name="Custom name")
def decorated_named_output_guardrail(
    context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(
        output_info="test_4",
        tripwire_triggered=False,
    )


@pytest.mark.asyncio
async def test_output_guardrail_decorators():
    guardrail = decorated_output_guardrail
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info == "test_3"

    guardrail = decorated_named_output_guardrail
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info == "test_4"
    assert guardrail.get_name() == "Custom name"

@pytest.mark.asyncio
async def test_input_guardrail_with_malicious_content():
    """Test input guardrail detection of potentially malicious content."""
    model = FakeModel()

    def input_content_filter(
        context: RunContextWrapper[Any], agent: Agent[Any], input: str
    ) -> GuardrailFunctionOutput:
        # Simple check for potentially malicious content
        malicious_patterns = [
            r"eval\s*\(",
            r"exec\s*\(",
            r"os\s*\.\s*system",
            r"subprocess",
            r"rm\s+-rf",
            r"DROP\s+TABLE",
            r"<script>",
            r"fetch\s*\(",
        ]

        for pattern in malicious_patterns:
            if re.search(pattern, input, re.IGNORECASE):
                return GuardrailFunctionOutput(
                    output_info={"reason": f"Detected potentially malicious pattern: {pattern}"},
                    tripwire_triggered=True
                )

        return GuardrailFunctionOutput(output_info={"message": "Input is safe"}, tripwire_triggered=False)

    agent = Agent(
        name="test",
        model=model,
        input_guardrails=[InputGuardrail(input_content_filter)],
    )

    model.set_next_output([get_text_message("Response")])

    # Test with benign input - should succeed
    await Runner.run(agent, input="Hello, how are you?")

    # Test with potentially malicious input - should raise an exception
    with pytest.raises(InputGuardrailTripwireTriggered):
        await Runner.run(agent, input="Let me try os.system('rm -rf /')")


@pytest.mark.asyncio
async def test_output_guardrail_with_sensitive_information():
    """Test output guardrail detection of potentially sensitive information."""
    model = FakeModel()

    def output_content_filter(
        context: RunContextWrapper[Any], agent: Agent[Any], output: str
    ) -> GuardrailFunctionOutput:
        # Simple check for potentially sensitive content patterns
        sensitive_patterns = [
            r"\b(?:\d[ -]*?){13,16}\b",  # Credit card-like numbers
            r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",  # Email-like patterns
            r"\b(?:password|secret|api_?key|access_?token|auth_?token)[=:]\s*\S+\b",  # API keys / credentials
            r"sk-[a-zA-Z0-9]{20,}",  # OpenAI API key pattern
        ]

        for pattern in sensitive_patterns:
            if re.search(pattern, output, re.IGNORECASE):
                return GuardrailFunctionOutput(
                    output_info={"reason": f"Detected potentially sensitive information: {pattern}"},
                    tripwire_triggered=True
                )

        return GuardrailFunctionOutput(output_info={"message": "Output is safe"}, tripwire_triggered=False)

    agent = Agent(
        name="test",
        model=model,
        output_guardrails=[OutputGuardrail(output_content_filter)],
    )

    # Test with safe output - should succeed
    model.set_next_output([get_text_message("This is a safe response")])
    await Runner.run(agent, input="Tell me something safe")

    # Test with potentially sensitive information in output - should raise an exception
    model.set_next_output([get_text_message("My email is test@example.com and my CC is 4111-1111-1111-1111")])
    with pytest.raises(OutputGuardrailTripwireTriggered):
        await Runner.run(agent, input="What is your personal information?")


@pytest.mark.asyncio
async def test_command_injection_in_tool():
    """Test protection against command injection in tools."""
    model = FakeModel(tracing_enabled=True)

    @function_tool
    def execute_command(command: str) -> str:
        """
        Execute a shell command.
        This is intentionally vulnerable for testing command injection protection.
        """
        # In a real implementation, this should be carefully protected
        # For testing, we just log what would have happened
        if any(c in command for c in [";", "&&", "||", "`", "$", "|"]):
            raise ValueError("Potential command injection detected")

        # For the test, don't actually execute anything
        return f"Would have executed: {command}"

    agent = Agent(
        name="test",
        model=model,
        tools=[execute_command],
    )

    # Test with safe command
    model.set_next_output([
        get_function_tool_call("execute_command", json.dumps({"command": "ls -la"})),
        get_text_message("Command executed")
    ])

    result = await Runner.run(agent, input="List files")
    assert "ls -la" in str(result.new_items)

    # Test with command injection attempt - set up a ValueError to be raised directly
    model.set_next_output(ValueError("Potential command injection detected"))

    with pytest.raises(ValueError) as excinfo:
        await Runner.run(agent, input="List files and delete everything")

    assert "injection" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_path_traversal_in_file_operation():
    """Test protection against path traversal attacks."""
    model = FakeModel(tracing_enabled=True)

    @function_tool
    def read_file(filepath: str) -> str:
        """
        Read a file from the specified path.
        This is deliberately vulnerable for testing path traversal protection.
        """
        # Basic protection against path traversal
        normalized_path = os.path.normpath(filepath)
        if ".." in normalized_path or normalized_path.startswith("/"):
            raise ValueError("Path traversal attempt detected")

        # For the test, don't actually read anything
        return f"Would have read: {filepath}"

    agent = Agent(
        name="test",
        model=model,
        tools=[read_file],
    )

    # Test with safe path
    model.set_next_output([
        get_function_tool_call("read_file", json.dumps({"filepath": "data/safe_file.txt"})),
        get_text_message("File read")
    ])

    result = await Runner.run(agent, input="Read data file")
    assert "data/safe_file.txt" in str(result.new_items)

    # Test with path traversal attempt - set up a ValueError to be raised directly
    model.set_next_output(ValueError("Path traversal attempt detected"))

    with pytest.raises(ValueError) as excinfo:
        await Runner.run(agent, input="Read system file")

    assert "traversal" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_xss_in_html_output():
    """Test protection against XSS in HTML output."""
    model = FakeModel()

    def html_sanitizer_guardrail(
        context: RunContextWrapper[Any], agent: Agent[Any], output: str
    ) -> GuardrailFunctionOutput:
        # Check for potential XSS patterns
        xss_patterns = [
            r"<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>",
            r"javascript:",
            r"onerror=",
            r"onclick=",
            r"onload=",
            r"onmouseover=",
        ]

        for pattern in xss_patterns:
            if re.search(pattern, output, re.IGNORECASE):
                return GuardrailFunctionOutput(
                    output_info={"reason": f"Detected potential XSS: {pattern}"},
                    tripwire_triggered=True
                )

        return GuardrailFunctionOutput(output_info={"message": "HTML is safe"}, tripwire_triggered=False)

    agent = Agent(
        name="test",
        model=model,
        output_guardrails=[OutputGuardrail(html_sanitizer_guardrail)],
    )

    # Test with safe HTML - should succeed
    model.set_next_output([get_text_message("<p>This is <b>safe</b> HTML</p>")])
    await Runner.run(agent, input="Give me some HTML")

    # Test with XSS attempt - should raise an exception
    model.set_next_output([get_text_message("<div>Malicious <script>alert('XSS')</script> content</div>")])
    with pytest.raises(OutputGuardrailTripwireTriggered):
        await Runner.run(agent, input="Give me HTML with JavaScript")