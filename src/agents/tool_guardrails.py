from __future__ import annotations

import inspect
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Callable, Generic, Optional, overload

from typing_extensions import TypeVar

from .agent import Agent
from .tool_context import ToolContext
from .util._types import MaybeAwaitable
from openai.types.responses import ResponseFunctionToolCall


@dataclass
class ToolGuardrailFunctionOutput:
    """The output of a tool guardrail function.

    - `output_info`: Optional data about checks performed.
    - `tripwire_triggered`: Whether the guardrail was tripped.
    - `model_message`: Message to send back to the model as the tool output if tripped.
    """

    output_info: Any
    tripwire_triggered: bool
    model_message: Optional[str] = None


@dataclass
class ToolInputGuardrailData:
    """Input data passed to a tool input guardrail function."""

    context: ToolContext[Any]
    agent: Agent[Any]
    tool_call: ResponseFunctionToolCall


@dataclass
class ToolOutputGuardrailData(ToolInputGuardrailData):
    """Input data passed to a tool output guardrail function.

    Extends input data with the tool's output.
    """

    output: Any


TContext_co = TypeVar("TContext_co", bound=Any, covariant=True)


@dataclass
class ToolInputGuardrail(Generic[TContext_co]):
    """A guardrail that runs before a function tool is invoked."""

    guardrail_function: Callable[[ToolInputGuardrailData], MaybeAwaitable[ToolGuardrailFunctionOutput]]
    name: str | None = None

    def get_name(self) -> str:
        return self.name or self.guardrail_function.__name__

    async def run(
        self, data: ToolInputGuardrailData
    ) -> ToolGuardrailFunctionOutput:
        result = self.guardrail_function(data)
        if inspect.isawaitable(result):
            return await result  # type: ignore[return-value]
        return result  # type: ignore[return-value]


@dataclass
class ToolOutputGuardrail(Generic[TContext_co]):
    """A guardrail that runs after a function tool is invoked."""

    guardrail_function: Callable[[ToolOutputGuardrailData], MaybeAwaitable[ToolGuardrailFunctionOutput]]
    name: str | None = None

    def get_name(self) -> str:
        return self.name or self.guardrail_function.__name__

    async def run(
        self, data: ToolOutputGuardrailData
    ) -> ToolGuardrailFunctionOutput:
        result = self.guardrail_function(data)
        if inspect.isawaitable(result):
            return await result  # type: ignore[return-value]
        return result  # type: ignore[return-value]


# Decorators
_ToolInputFuncSync = Callable[[ToolInputGuardrailData], ToolGuardrailFunctionOutput]
_ToolInputFuncAsync = Callable[[ToolInputGuardrailData], Awaitable[ToolGuardrailFunctionOutput]]


@overload
def tool_input_guardrail(func: _ToolInputFuncSync):  # type: ignore[overload-overlap]
    ...


@overload
def tool_input_guardrail(func: _ToolInputFuncAsync):  # type: ignore[overload-overlap]
    ...


@overload
def tool_input_guardrail(*, name: str | None = None) -> Callable[[
    _ToolInputFuncSync | _ToolInputFuncAsync
], ToolInputGuardrail[Any]]: ...


def tool_input_guardrail(
    func: _ToolInputFuncSync | _ToolInputFuncAsync | None = None,
    *,
    name: str | None = None,
) -> ToolInputGuardrail[Any] | Callable[[
    _ToolInputFuncSync | _ToolInputFuncAsync
], ToolInputGuardrail[Any]]:
    """Decorator to create a ToolInputGuardrail from a function."""

    def decorator(f: _ToolInputFuncSync | _ToolInputFuncAsync) -> ToolInputGuardrail[Any]:
        return ToolInputGuardrail(guardrail_function=f, name=name or f.__name__)

    if func is not None:
        return decorator(func)
    return decorator


_ToolOutputFuncSync = Callable[[ToolOutputGuardrailData], ToolGuardrailFunctionOutput]
_ToolOutputFuncAsync = Callable[[ToolOutputGuardrailData], Awaitable[ToolGuardrailFunctionOutput]]


@overload
def tool_output_guardrail(func: _ToolOutputFuncSync):  # type: ignore[overload-overlap]
    ...


@overload
def tool_output_guardrail(func: _ToolOutputFuncAsync):  # type: ignore[overload-overlap]
    ...


@overload
def tool_output_guardrail(*, name: str | None = None) -> Callable[[
    _ToolOutputFuncSync | _ToolOutputFuncAsync
], ToolOutputGuardrail[Any]]: ...


def tool_output_guardrail(
    func: _ToolOutputFuncSync | _ToolOutputFuncAsync | None = None,
    *,
    name: str | None = None,
) -> ToolOutputGuardrail[Any] | Callable[[
    _ToolOutputFuncSync | _ToolOutputFuncAsync
], ToolOutputGuardrail[Any]]:
    """Decorator to create a ToolOutputGuardrail from a function."""

    def decorator(f: _ToolOutputFuncSync | _ToolOutputFuncAsync) -> ToolOutputGuardrail[Any]:
        return ToolOutputGuardrail(guardrail_function=f, name=name or f.__name__)

    if func is not None:
        return decorator(func)
    return decorator

