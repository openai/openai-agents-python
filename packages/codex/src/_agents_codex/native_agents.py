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
    overload as overload,
)

from typing_extensions import TypedDict as TypedDict, TypeVar as TypeVar

from agents.config import JsonValue as JsonValue

from ._events import SessionItem as SessionItem
from ._public import preserve_module_identity as _preserve_module_identity
from .native_interception import NativeToolCall as NativeToolCall, StopTurn
from .observation import Observation as Observation
from .turn import _child_submission_id as _child_submission_id

if TYPE_CHECKING:
    from ._function_tools import Tool, ToolContext
    from .decisions import DecisionInput, DecisionProvider
    from .executor import Environment
    from .tools import ToolRef

_DecisionT = TypeVar("_DecisionT")
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


class NativeRoleToolInvocation(TypedDict):
    """The exact native call and physical child turn of a managed tool."""

    tool_name: str
    role: str
    input: str
    native_call_id: str
    parent_thread_id: str
    parent_turn_id: str
    child_thread_id: str
    child_turn_id: str
    cell_id: str | None
    runtime_tool_call_id: str | None


class NativeRoleToolStreamEvent(TypedDict):
    event: Observation
    invocation: NativeRoleToolInvocation


class NativeRoleToolResult(TypedDict):
    """A successful managed child result, detached from its native capture."""

    role: str
    thread_id: str
    turn_id: str
    final_output: JsonValue
    items: tuple[SessionItem, ...]


@dataclass(frozen=True, kw_only=True)
class NativeRoleTool:
    """Expose a declared role as a tool that returns its child turn result.

    on_stream receives ordered observations with the original input and exact
    parent/child call identity. The parent waits for its queued callbacks before
    accepting the result. Ordinary handler exceptions are logged. The pending
    limit excludes the running handler; None allows an unlimited backlog.
    Overflow cancels that child and withholds its result.

    extract_result converts a successful child's final output and recorded items
    into the string returned to its parent. An extractor error becomes a
    recoverable tool failure. Callbacks cannot mutate their own Session.
    """

    name: str
    role: str
    description: str
    on_stream: Callable[[NativeRoleToolStreamEvent], None | Awaitable[None]] | None = field(
        default=None, repr=False
    )
    on_stream_max_pending_events: int | None = 1024
    extract_result: Callable[[NativeRoleToolResult], str | Awaitable[str]] | None = field(
        default=None, repr=False
    )

    def __post_init__(self) -> None:
        for callback in (self.on_stream, self.extract_result):
            if callback is not None and not callable(callback):
                raise TypeError("Managed role callbacks must be callable")
        limit = self.on_stream_max_pending_events
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        ):
            raise ValueError("on_stream_max_pending_events must be a positive integer or None")
        if (
            not isinstance(self.name, str)
            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.name) is None
        ):
            raise ValueError("Native role tool names must start with a lowercase ASCII letter")
        if self.name in _COLLABORATION_TOOLS or self.name == "close_agent":
            raise ValueError("Built-in collaboration tools cannot be replaced")
        for name in ("role", "description"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Native role tool {name} must be a nonblank string")


def _native_agent_options(declaration: NativeAgents[Any] | None) -> dict[str, Any]:
    if declaration is None:
        return {}
    tools = []
    for tool in declaration.managed_tools:
        spec: dict[str, Any] = {
            "name": tool.name,
            "role": tool.role,
            "description": tool.description,
        }
        if tool.on_stream is not None or tool.extract_result is not None:
            spec.update(result_control=True, capture_items=tool.extract_result is not None)
        tools.append(spec)
    return {
        "_native_agent_roles": [role.name for role in declaration.additional_roles],
        "_native_agent_max_depth": declaration.max_depth,
        **({"_native_agent_tools": tools} if tools else {}),
    }


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
    """Enable native V2 children sharing the Session's granted tools.

    The Environment must grant all three executor tools. With
    project_guidance=True at Session opening, children also share workspace
    image viewing under the same executor path grants.

    Include this declaration in Agent tools. Supply the Environment at session opening.
    NativeAgents reserves the collaboration namespace for native tools. Custom
    functions may use that namespace only when NativeAgents is not enabled.
    Core's default, explorer, and worker roles are available without additional
    roles. Additional roles select instructions and optional model and
    reasoning effort.
    managed_tools exposes selected roles as named collaboration tools. Each
    call waits for its own child turn and returns that turn's result.
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
    managed_tools: Sequence[NativeRoleTool] = ()

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
        managed_tools = tuple(self.managed_tools)
        if any(not isinstance(tool, NativeRoleTool) for tool in managed_tools):
            raise TypeError("managed_tools must contain NativeRoleTool declarations")
        if len({tool.name for tool in managed_tools}) != len(managed_tools):
            raise ValueError("Native role tool names must be unique")
        role_names = _BUILTIN_ROLES | {role.name for role in roles}
        for tool in managed_tools:
            if tool.role not in role_names:
                raise ValueError(f"Unknown native role {tool.role!r} for tool {tool.name!r}")
        object.__setattr__(self, "managed_tools", managed_tools)
        if isinstance(self.max_depth, bool) or not isinstance(self.max_depth, int):
            raise TypeError("max_depth must be an integer")
        if not 1 <= self.max_depth < sys.maxsize:
            raise ValueError("max_depth must be a positive native-sized integer")
        if self.max_children is not None:
            if isinstance(self.max_children, bool) or not isinstance(self.max_children, int):
                raise TypeError("max_children must be an integer or None")
            if not 1 <= self.max_children < sys.maxsize:
                raise ValueError("max_children must be a positive native-sized integer")

    @property
    def _collaboration_tool_names(self) -> tuple[str, ...]:
        return (*_COLLABORATION_TOOLS, *(tool.name for tool in self.managed_tools))


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

    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        choices: None = None,
        output_type: None = None,
    ) -> bool: ...

    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        choices: Sequence[_DecisionT],
        output_type: None = None,
    ) -> _DecisionT: ...

    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        output_type: type[_DecisionT],
        choices: None = None,
    ) -> _DecisionT: ...

    @overload
    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        output_type: Any,
        choices: None = None,
    ) -> Any: ...

    async def decide(
        self,
        question: str,
        *,
        input: DecisionInput,
        provider: DecisionProvider | None = None,
        choices: Sequence[Any] | None = None,
        output_type: Any = None,
    ) -> Any:
        """Answer a typed question during this exact admitted callback.

        Input is explicit. The callback's captured provider is the default;
        provider overrides it for this call. Completed usage is recorded before
        local validation. For overlapping decisions, await ``asyncio.gather()`` in this callback.
        """
        from .decisions import _bound_decide

        return await _bound_decide(
            self, question, input=input, provider=provider, choices=choices, output_type=output_type
        )


_preserve_module_identity(globals(), "agents.native_agents")
