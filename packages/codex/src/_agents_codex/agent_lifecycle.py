"""Application callbacks at admitted Agent lifecycle boundaries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass as dataclass, field
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeAlias, TypeVar

from ._compiled_dataclasses import dataclass as _sdk_dataclass
from ._public import preserve_module_identity

if TYPE_CHECKING:
    from .agent import Agent

ContextT = TypeVar("ContextT")


@_sdk_dataclass(frozen=True)
class AgentLifecycleContext(Generic[ContextT]):
    """The live Agent owner at an awaited start, handoff, or accepted output."""

    kind: Literal["start", "handoff", "end"]
    context: ContextT = field(repr=False)
    agent: Agent[ContextT, Any] = field(repr=False)
    session_id: str
    turn_id: str
    subagent_id: str | None = None
    target: Agent[ContextT, Any] | None = field(default=None, repr=False)
    output: Any = field(default=None, repr=False)


_AgentLifecycleCallback: TypeAlias = Callable[
    [AgentLifecycleContext[ContextT]], None | Awaitable[None]
]
preserve_module_identity(globals(), "agents.agent_lifecycle")
