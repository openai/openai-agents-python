from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Generic, Union

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
    MaybeAwaitable[Union[RunErrorHandlerResult, dict[str, Any], Any, None]],
]


class RunErrorHandlers(TypedDict, Generic[TContext], total=False):
    """Error handlers keyed by error kind."""

    max_turns: RunErrorHandler[TContext]


__all__ = [
    "RunErrorData",
    "RunErrorHandler",
    "RunErrorHandlerInput",
    "RunErrorHandlerResult",
    "RunErrorHandlers",
]
