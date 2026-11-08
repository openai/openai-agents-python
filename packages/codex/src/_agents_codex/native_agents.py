"""Declarations and live turn references for native model-selected children."""

from __future__ import annotations

import re as re
import sys as sys
from collections.abc import (
    Awaitable as Awaitable,
    Callable as Callable,
    Mapping as Mapping,
    Sequence as Sequence,
)
from dataclasses import dataclass as dataclass, field as field
from types import MappingProxyType as MappingProxyType
from typing import (
    TYPE_CHECKING as TYPE_CHECKING,
    Any as Any,
    Generic as Generic,
    Literal as Literal,
    Protocol as Protocol,
    TypeAlias as TypeAlias,
)

from typing_extensions import TypeVar as TypeVar

from ._public import preserve_module_identity as _preserve_module_identity
from .native_interception import NativeToolCall as NativeToolCall, StopTurn
from .turn import _child_submission_id as _child_submission_id

if TYPE_CHECKING:
    from ._function_tools import Tool, ToolContext
    from .executor import Environment
    from .tools import ToolRef

ContextT = TypeVar("ContextT", contravariant=True, default=object)
_ContextT_co = TypeVar("_ContextT_co", covariant=True, default=object)

_BUILTIN_ROLES = frozenset({"default", "explorer", "worker"})
_COLLABORATION_TOOLS = (
    "spawn_agent",
    "wait_agent",
    "send_message",
    "followup_task",
    "interrupt_agent",
    "list_agents",
)


@dataclass(frozen=True, kw_only=True)
class NativeRole:
    """Add a named native role with its own developer instructions.

    Model and reasoning default to the parent. An application may select a
    compatible model and reasoning effort for a role at Session startup.
    Provider and tool authority remain shared. Built-in names cannot be replaced.
    """

    name: str
    instructions: str = field(repr=False)
    description: str
    model: str | None = None
    model_settings: Mapping[Literal["reasoning_effort"], str] | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.name) is None
        ):
            raise ValueError("Native role names must contain 1–64 ASCII letters, digits, _ or -")
        if self.name in _BUILTIN_ROLES:
            raise ValueError("Built-in native roles cannot be replaced")
        for name in ("instructions", "description"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Native role {name} must be a nonblank string")
        if self.model is not None and (not isinstance(self.model, str) or not self.model.strip()):
            raise ValueError("Native role model must be a nonblank string or None")
        if self.model_settings is not None:
            settings = dict(self.model_settings)
            if settings.keys() - {"reasoning_effort"}:
                raise ValueError("NativeRole model_settings supports only reasoning_effort")
            if "reasoning_effort" in settings and (
                not isinstance(settings["reasoning_effort"], str)
                or not settings["reasoning_effort"].strip()
            ):
                raise ValueError("Native role reasoning_effort must be a nonblank string")
            object.__setattr__(self, "model_settings", MappingProxyType(settings))


_ChildStepResult: TypeAlias = (
    Sequence["ToolRef | Tool[Any] | Environment[Any] | NativeAgents[Any]"]
    | None
    | StopTurn
    | Awaitable[
        Sequence["ToolRef | Tool[Any] | Environment[Any] | NativeAgents[Any]"] | None | StopTurn
    ]
)

_ChildBeforeStep: TypeAlias = Callable[["ChildStepContext[ContextT]"], _ChildStepResult]


@dataclass(frozen=True, kw_only=True)
class NativeAgents(Generic[ContextT]):
    """Enable native V2 children with the Session's three executor tools.

    Include this declaration in Agent tools. Supply the Environment at session opening.
    NativeAgents reserves the collaboration namespace for native tools. Custom
    functions may use that namespace only when NativeAgents is not enabled.
    Core's default, explorer, and worker roles are available without additional
    roles. Additional roles select instructions and optional model and
    reasoning effort.
    max_children=None uses Core's session limit; max_depth=1 permits only
    direct children. Larger depths permit bounded delegation.
    Children share the Session's declared Python tools, application context,
    MCP tools and policies. This surface requires an explicit compatible model.
    Child output is always text. Root output follows Agent.
    interactions=True permits owned child approvals and forms through Session
    interactions or on_interaction. HTTP forms require native request association;
    uncorrelated HTTP background forms remain unowned. In this opt-in mode each
    child admission starts its own stdio connections and joins them before the
    admission ends. Process-local tool state and subscriptions end with that
    admission; external state and child conversation history can persist. This
    adds process startup and catalog validation to each child turn. Initial
    unowned startup prompts fail child creation before registration. Root and
    default child connection lifetimes retain their existing behavior. The
    default remains headless refusal.
    Idle checkpoints retain child conversation and task history, but not live
    turn handles.
    Tool replacement requires all native children to have closed.

    ``approval(context, call)`` allows or denies collaboration before native
    dispatch. It receives root and descendant calls; use the context's turn
    identity and application context to select policy. Native child admission
    still applies after approval.

    ``before_step(step)`` selects admitted tools before each
    native child model request. step.child is a ChildInvocation for that exact
    incarnation and turn. step.context is the current Session application context.
    None restores its captured operation defaults; []
    disables every model tool. Root submissions use their own before_step.
    Return StopTurn to stop only this child turn before its next model request
    and join its owned cleanup. The parent and sibling turns keep running;
    the child can accept a new turn afterward. The stopped turn retains the
    existing NativeError policy-stop outcome. step_index counts ordinary step
    preparations, not transport retries, compaction or output-repair requests.

    """

    additional_roles: Sequence[NativeRole] = ()
    max_children: int | None = None
    approval: (
        Callable[[ToolContext[object], NativeToolCall], bool | Awaitable[bool]]
        | Callable[[ToolContext[ContextT], NativeToolCall], bool | Awaitable[bool]]
        | None
    ) = field(default=None, repr=False)
    max_depth: int = 1
    before_step: (
        Callable[[ChildStepContext[object]], _ChildStepResult] | _ChildBeforeStep[ContextT] | None
    ) = field(default=None, repr=False)
    interactions: bool = False

    def __post_init__(self) -> None:
        if self.before_step is not None and not callable(self.before_step):
            raise TypeError("NativeAgents before_step must be callable")
        if not isinstance(self.interactions, bool):
            raise TypeError("NativeAgents interactions must be a boolean")
        if self.approval is not None and not callable(self.approval):
            raise TypeError("NativeAgents approval must be callable")
        roles = tuple(self.additional_roles)
        if any(not isinstance(role, NativeRole) for role in roles):
            raise TypeError("additional_roles must contain NativeRole declarations")
        if len({role.name for role in roles}) != len(roles):
            raise ValueError("Additional native role names must be unique")
        object.__setattr__(self, "additional_roles", roles)
        if isinstance(self.max_depth, bool) or not isinstance(self.max_depth, int):
            raise TypeError("max_depth must be an integer")
        if not 1 <= self.max_depth < sys.maxsize:
            raise ValueError("max_depth must be a positive native-sized integer")
        if self.max_children is not None:
            if isinstance(self.max_children, bool) or not isinstance(self.max_children, int):
                raise TypeError("max_children must be an integer or None")
            if not 1 <= self.max_children < sys.maxsize:
                raise ValueError("max_children must be a positive native-sized integer")


@dataclass(frozen=True, slots=True)
class ChildInvocation:
    """Identity of a native child's application callback, without execution authority.

    A callback can arrive before the event reader publishes a Turn.
    Use Session.subagents() to obtain a Turn for waiting.
    The instance identifies this live child incarnation and is not restorable.
    """

    session_id: str
    child_id: str
    instance_id: str
    turn_id: str

    @property
    def submission_id(self) -> str:
        """Stable observation identity; not an application submission handle."""
        return _child_submission_id(self.child_id, self.instance_id, self.turn_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class ChildStepContext(Generic[_ContextT_co]):
    """Current application context and exact child preparation identity."""

    context: _ContextT_co = field(repr=False)
    child: ChildInvocation
    step_index: int


_preserve_module_identity(globals(), "agents.native_agents")
