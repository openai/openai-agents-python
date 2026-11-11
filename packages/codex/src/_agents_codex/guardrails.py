"""Named checks and local diagnostics for native runs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar

from agents.config import JsonValue

from ._public import preserve_module_identity as _preserve_module_identity
from .completion import CompletionContext
from .tools import ToolCallInfo

if TYPE_CHECKING:
    from .agent import Agent
    from .input import UserInput
    from .native_agents import ChildStepContext
    from .turn import Turn

ContextT = TypeVar("ContextT")


@dataclass(frozen=True)
class GuardrailResult:
    """A tripwire verdict and application-local diagnostic information."""

    tripwire_triggered: bool
    output_info: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.tripwire_triggered) is not bool:
            raise TypeError("tripwire_triggered must be a bool")


@dataclass(frozen=True)
class InputGuardrail(Generic[ContextT]):
    """Initial-input checks with explicit native-child admission.

    Root callback: the public Turn and original UserInput. Optional child callback:
    the existing child step context and original Core user/communication records.
    Parallel checks: overlap with model work; blocking checks: a concurrent group
    before the first model step. Independent declarations for each child callback.
    """

    name: str
    callback: Callable[
        [Turn[Any], ContextT, Agent[Any, Any], UserInput],
        GuardrailResult | Awaitable[GuardrailResult],
    ] = field(repr=False)
    run_in_parallel: bool = True
    child_callback: (
        Callable[
            [ChildStepContext[ContextT], Agent[Any, Any], Sequence[dict[str, JsonValue]]],
            GuardrailResult | Awaitable[GuardrailResult],
        ]
        | None
    ) = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _validate_check(self.name, self.callback)
        if self.child_callback is not None and not callable(self.child_callback):
            raise TypeError("child_callback must be callable")
        if type(self.run_in_parallel) is not bool:
            raise TypeError("run_in_parallel must be a bool")


@dataclass(frozen=True)
class OutputGuardrail(Generic[ContextT]):
    """A check of the converted final output, before native acceptance."""

    name: str
    callback: Callable[
        [CompletionContext[ContextT], Agent[Any, Any]],
        GuardrailResult | Awaitable[GuardrailResult],
    ] = field(repr=False)

    def __post_init__(self) -> None:
        _validate_check(self.name, self.callback)


def _validate_check(name: str, callback: Any) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Guardrail name must be a nonempty string")
    if not callable(callback):
        raise TypeError("Guardrail callback must be callable")


@dataclass(frozen=True)
class GuardrailRecord:
    """One completed check on its actual run and source Agent."""

    kind: Literal["input", "output"]
    name: str
    result: GuardrailResult
    agent: Agent[Any, Any] = field(repr=False)
    session_id: str
    turn_id: str
    subagent_id: str | None = None
    tool_call: ToolCallInfo | None = None


class GuardrailTripwire(RuntimeError):
    """A local snapshot of completed checks, including the tripping check."""

    def __init__(self, record: GuardrailRecord, results: tuple[GuardrailRecord, ...]) -> None:
        super().__init__("Run blocked by guardrail")
        self.record = record
        self.results = results


_preserve_module_identity(globals(), "agents.guardrails")
