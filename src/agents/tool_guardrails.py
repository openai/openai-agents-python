from __future__ import annotations

import inspect
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Generic, overload

from openai.types.responses import ResponseFunctionToolCall
from typing_extensions import TypeVar

from .exceptions import UserError
from .tool_context import ToolContext
from .util._types import MaybeAwaitable

if TYPE_CHECKING:
    from .agent import Agent


@dataclass
class ToolGuardrailFunctionOutput:
    """The output of a tool guardrail function."""

    output_info: Any
    """
    Optional data about checks performed. For example, the guardrail could include
    information about the checks it performed and granular results.
    """

    tripwire_triggered: bool
    """
    Whether the tripwire was triggered. If triggered, the tool execution will be halted.
    """

    model_message: str | None = None
    """
    Message to send back to the model as the tool output if tripped.
    """


@dataclass
class ToolInputGuardrailData:
    """Input data passed to a tool input guardrail function."""

    context: ToolContext[Any]
    """
    The tool context containing information about the current tool execution.
    """

    agent: Agent[Any]
    """
    The agent that is executing the tool.
    """

    tool_call: ResponseFunctionToolCall
    """
    The tool call data including the function name and arguments.
    """


@dataclass
class ToolOutputGuardrailData(ToolInputGuardrailData):
    """Input data passed to a tool output guardrail function.

    Extends input data with the tool's output.
    """

    output: Any
    """
    The output produced by the tool function.
    """


TContext_co = TypeVar("TContext_co", bound=Any, covariant=True)


@dataclass
class ToolInputGuardrail(Generic[TContext_co]):
    """A guardrail that runs before a function tool is invoked."""

    guardrail_function: Callable[
        [ToolInputGuardrailData], MaybeAwaitable[ToolGuardrailFunctionOutput]
    ]
    """
    The function that implements the guardrail logic.
    """

    name: str | None = None
    """
    Optional name for the guardrail. If not provided, uses the function name.
    """

    def get_name(self) -> str:
        return self.name or self.guardrail_function.__name__

    async def run(self, data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        if not callable(self.guardrail_function):
            raise UserError(f"Guardrail function must be callable, got {self.guardrail_function}")

        result = self.guardrail_function(data)
        if inspect.isawaitable(result):
            return await result
        return result


@dataclass
class ToolOutputGuardrail(Generic[TContext_co]):
    """A guardrail that runs after a function tool is invoked."""

    guardrail_function: Callable[
        [ToolOutputGuardrailData], MaybeAwaitable[ToolGuardrailFunctionOutput]
    ]
    """
    The function that implements the guardrail logic.
    """

    name: str | None = None
    """
    Optional name for the guardrail. If not provided, uses the function name.
    """

    def get_name(self) -> str:
        return self.name or self.guardrail_function.__name__

    async def run(self, data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        if not callable(self.guardrail_function):
            raise UserError(f"Guardrail function must be callable, got {self.guardrail_function}")

        result = self.guardrail_function(data)
        if inspect.isawaitable(result):
            return await result
        return result


# Decorators
_ToolInputFuncSync = Callable[[ToolInputGuardrailData], ToolGuardrailFunctionOutput]
_ToolInputFuncAsync = Callable[[ToolInputGuardrailData], Awaitable[ToolGuardrailFunctionOutput]]


@overload
def tool_input_guardrail(func: _ToolInputFuncSync): ...


@overload
def tool_input_guardrail(func: _ToolInputFuncAsync): ...


@overload
def tool_input_guardrail(
    *, name: str | None = None
) -> Callable[[_ToolInputFuncSync | _ToolInputFuncAsync], ToolInputGuardrail[Any]]: ...


def tool_input_guardrail(
    func: _ToolInputFuncSync | _ToolInputFuncAsync | None = None,
    *,
    name: str | None = None,
) -> (
    ToolInputGuardrail[Any]
    | Callable[[_ToolInputFuncSync | _ToolInputFuncAsync], ToolInputGuardrail[Any]]
):
    """Decorator to create a ToolInputGuardrail from a function."""

    def decorator(f: _ToolInputFuncSync | _ToolInputFuncAsync) -> ToolInputGuardrail[Any]:
        return ToolInputGuardrail(guardrail_function=f, name=name or f.__name__)

    if func is not None:
        return decorator(func)
    return decorator


_ToolOutputFuncSync = Callable[[ToolOutputGuardrailData], ToolGuardrailFunctionOutput]
_ToolOutputFuncAsync = Callable[[ToolOutputGuardrailData], Awaitable[ToolGuardrailFunctionOutput]]


@overload
def tool_output_guardrail(func: _ToolOutputFuncSync): ...


@overload
def tool_output_guardrail(func: _ToolOutputFuncAsync): ...


@overload
def tool_output_guardrail(
    *, name: str | None = None
) -> Callable[[_ToolOutputFuncSync | _ToolOutputFuncAsync], ToolOutputGuardrail[Any]]: ...


def tool_output_guardrail(
    func: _ToolOutputFuncSync | _ToolOutputFuncAsync | None = None,
    *,
    name: str | None = None,
) -> (
    ToolOutputGuardrail[Any]
    | Callable[[_ToolOutputFuncSync | _ToolOutputFuncAsync], ToolOutputGuardrail[Any]]
):
    """Decorator to create a ToolOutputGuardrail from a function."""

    def decorator(f: _ToolOutputFuncSync | _ToolOutputFuncAsync) -> ToolOutputGuardrail[Any]:
        return ToolOutputGuardrail(guardrail_function=f, name=name or f.__name__)

    if func is not None:
        return decorator(func)
    return decorator
