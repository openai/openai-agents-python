from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent import Agent
    from .guardrail import InputGuardrailResult, OutputGuardrailResult
    from .items import ModelResponse, RunItem, TResponseInputItem
    from .run_context import RunContextWrapper
    from .tool_guardrails import (
        ToolGuardrailFunctionOutput,
        ToolInputGuardrail,
        ToolOutputGuardrail,
    )

from .util._pretty_print import pretty_print_run_error_details


@dataclass
class RunErrorDetails:
    """Data collected from an agent run when an exception occurs."""

    input: str | list[TResponseInputItem]
    new_items: list[RunItem]
    raw_responses: list[ModelResponse]
    last_agent: Agent[Any]
    context_wrapper: RunContextWrapper[Any]
    input_guardrail_results: list[InputGuardrailResult]
    output_guardrail_results: list[OutputGuardrailResult]

    def __str__(self) -> str:
        return pretty_print_run_error_details(self)


class AgentsException(Exception):
    """Base class for all exceptions in the Agents SDK."""

    run_data: RunErrorDetails | None

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.run_data = None


class MaxTurnsExceeded(AgentsException):
    """Exception raised when the maximum number of turns is exceeded."""

    message: str

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class ModelBehaviorError(AgentsException):
    """Exception raised when the model does something unexpected, e.g. calling a tool that doesn't
    exist, or providing malformed JSON.
    """

    message: str

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class UserError(AgentsException):
    """Exception raised when the user makes an error using the SDK."""

    message: str

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class InputGuardrailTripwireTriggered(AgentsException):
    """Exception raised when a guardrail tripwire is triggered."""

    guardrail_result: InputGuardrailResult
    """The result data of the guardrail that was triggered."""

    def __init__(self, guardrail_result: InputGuardrailResult):
        self.guardrail_result = guardrail_result
        super().__init__(
            f"Guardrail {guardrail_result.guardrail.__class__.__name__} triggered tripwire"
        )


class OutputGuardrailTripwireTriggered(AgentsException):
    """Exception raised when a guardrail tripwire is triggered."""

    guardrail_result: OutputGuardrailResult
    """The result data of the guardrail that was triggered."""

    def __init__(self, guardrail_result: OutputGuardrailResult):
        self.guardrail_result = guardrail_result
        super().__init__(
            f"Guardrail {guardrail_result.guardrail.__class__.__name__} triggered tripwire"
        )


class ToolInputGuardrailTripwireTriggered(AgentsException):
    """Exception raised when a tool input guardrail tripwire is triggered."""

    guardrail: ToolInputGuardrail[Any]
    """The guardrail that was triggered."""

    output: ToolGuardrailFunctionOutput
    """The output from the guardrail function."""

    def __init__(self, guardrail: ToolInputGuardrail[Any], output: ToolGuardrailFunctionOutput):
        self.guardrail = guardrail
        self.output = output
        super().__init__(f"Tool input guardrail {guardrail.__class__.__name__} triggered tripwire")


class ToolOutputGuardrailTripwireTriggered(AgentsException):
    """Exception raised when a tool output guardrail tripwire is triggered."""

    guardrail: ToolOutputGuardrail[Any]
    """The guardrail that was triggered."""

    output: ToolGuardrailFunctionOutput
    """The output from the guardrail function."""

    def __init__(self, guardrail: ToolOutputGuardrail[Any], output: ToolGuardrailFunctionOutput):
        self.guardrail = guardrail
        self.output = output
        super().__init__(f"Tool output guardrail {guardrail.__class__.__name__} triggered tripwire")


class SessionError(AgentsException):
    """Exception raised when a session operation fails.

    This is the base class for all session-related exceptions, providing
    consistent error handling for session storage, retrieval, and manipulation.
    """

    session_id: str | None
    """The ID of the session that caused the error, if available."""

    def __init__(self, message: str, session_id: str | None = None):
        self.session_id = session_id
        self.message = message
        if session_id:
            super().__init__(f"Session '{session_id}': {message}")
        else:
            super().__init__(message)


class SessionNotFoundError(SessionError):
    """Exception raised when a session cannot be found.

    This exception is raised when attempting to retrieve or manipulate
    a session that does not exist in the storage backend.
    """

    def __init__(self, session_id: str):
        super().__init__(f"Session not found", session_id=session_id)


class SessionSerializationError(SessionError):
    """Exception raised when session data cannot be serialized or deserialized.

    This exception is raised when session items contain data that cannot
    be properly converted to/from the storage format.
    """

    def __init__(self, message: str, session_id: str | None = None):
        super().__init__(f"Serialization error: {message}", session_id=session_id)

