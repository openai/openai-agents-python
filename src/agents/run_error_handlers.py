from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Generic

from typing_extensions import TypedDict

from .agent import Agent
from .exceptions import MaxTurnsExceeded
from .items import ModelResponse, RunItem, TResponseInputItem
from .run_context import RunContextWrapper, TContext
from .util._types import MaybeAwaitable


@dataclass
class RunErrorData:
    """Snapshot of run data passed to error handlers."""

    input: str | list[TResponseInputItem]
    new_items: list[RunItem]
    history: list[TResponseInputItem]
    output: list[TResponseInputItem]
    raw_responses: list[ModelResponse]
    last_agent: Agent[Any]


@dataclass
class RunErrorHandlerInput(Generic[TContext]):
    error: MaxTurnsExceeded
    context: RunContextWrapper[TContext]
    run_data: RunErrorData


@dataclass
class RunErrorHandlerResult:
    """Result returned by an error handler."""

    final_output: Any
    include_in_history: bool = True


# Handlers may return RunErrorHandlerResult, a dict with final_output, or a raw final output value.
RunErrorHandler = Callable[
    [RunErrorHandlerInput[TContext]],
    MaybeAwaitable[RunErrorHandlerResult | dict[str, Any] | Any | None],
]


@dataclass
class ToolNotFoundErrorHandlerInput(Generic[TContext]):
    """Input passed to the ``tool_not_found`` error handler.

    The handler is invoked when the model calls a tool that is not registered on the current
    agent. Returning :class:`ToolNotFoundAction` tells the runner to inject a synthetic tool
    output with ``error_message`` so the model can self-correct on the next turn. Returning
    ``None`` re-raises the original :class:`ModelBehaviorError`.
    """

    tool_name: str
    """Name of the tool the model tried to call."""

    available_tools: list[str]
    """Names of tools actually registered on the agent (function + custom + handoffs)."""

    agent: Agent[Any]
    """The agent that received the bogus tool call."""

    context: RunContextWrapper[TContext]
    """The run context wrapper."""

    run_data: RunErrorData
    """Snapshot of run data at the moment the error occurred."""


@dataclass
class ToolNotFoundAction:
    """Instructs the runner to recover from a tool-not-found error.

    The runner appends a synthetic ``function_call_output`` item containing ``error_message`` to
    the conversation, then continues the turn. The model will see the error on its next step and
    can retry with a valid tool name.

    Note: recovery is bounded by the run's ``max_turns`` setting. A model that repeatedly
    hallucinates tool calls will eventually hit that limit and raise ``MaxTurnsExceeded``.
    """

    error_message: str


ToolNotFoundErrorHandler = Callable[
    [ToolNotFoundErrorHandlerInput[TContext]],
    MaybeAwaitable["ToolNotFoundAction | None"],
]


class RunErrorHandlers(TypedDict, Generic[TContext], total=False):
    """Error handlers keyed by error kind."""

    max_turns: RunErrorHandler[TContext]
    tool_not_found: ToolNotFoundErrorHandler[TContext]


__all__ = [
    "RunErrorData",
    "RunErrorHandler",
    "RunErrorHandlerInput",
    "RunErrorHandlerResult",
    "RunErrorHandlers",
    "ToolNotFoundAction",
    "ToolNotFoundErrorHandler",
    "ToolNotFoundErrorHandlerInput",
]
