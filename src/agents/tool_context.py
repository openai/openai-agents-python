from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, cast

from openai.types.responses import ResponseFunctionToolCall

from ._tool_identity import get_tool_call_namespace, tool_trace_name
from .agent_tool_state import get_agent_tool_state_scope, set_agent_tool_state_scope
from .run_context import RunContextWrapper, TContext
from .usage import Usage

if TYPE_CHECKING:
    from .agent import AgentBase
    from .items import TResponseInputItem
    from .run_config import RunConfig
    from .run_context import _ApprovalRecord


def _assert_must_pass_tool_call_id() -> str:
    raise ValueError("tool_call_id must be passed to ToolContext")


def _assert_must_pass_tool_name() -> str:
    raise ValueError("tool_name must be passed to ToolContext")


def _assert_must_pass_tool_arguments() -> str:
    raise ValueError("tool_arguments must be passed to ToolContext")


_MISSING = object()


@dataclass
class ToolContext(RunContextWrapper[TContext]):
    """The context of a tool call."""

    tool_name: str = field(default_factory=_assert_must_pass_tool_name)
    """The name of the tool being invoked."""

    tool_call_id: str = field(default_factory=_assert_must_pass_tool_call_id)
    """The ID of the tool call."""

    tool_arguments: str = field(default_factory=_assert_must_pass_tool_arguments)
    """The raw arguments string of the tool call."""

    tool_call: ResponseFunctionToolCall | None = None
    """The tool call object associated with this invocation."""

    tool_namespace: str | None = None
    """The Responses API namespace for this tool call, when present."""

    agent: AgentBase[Any] | None = None
    """The active agent for this tool call, when available."""

    run_config: RunConfig | None = None
    """The active run config for this tool call, when available."""

    def __init__(
        self,
        context: TContext,
        usage: Usage | object = _MISSING,
        tool_name: str | object = _MISSING,
        tool_call_id: str | object = _MISSING,
        tool_arguments: str | object = _MISSING,
        tool_call: ResponseFunctionToolCall | None = None,
        *,
        tool_namespace: str | None = None,
        agent: AgentBase[Any] | None = None,
        run_config: RunConfig | None = None,
        turn_input: list[TResponseInputItem] | None = None,
        _approvals: dict[str, _ApprovalRecord] | None = None,
        tool_input: Any | None = None,
    ) -> None:
        """Preserve the v0.7 positional constructor while accepting new context fields."""
        resolved_usage = Usage() if usage is _MISSING else cast(Usage, usage)
        super().__init__(
            context=context,
            usage=resolved_usage,
            turn_input=list(turn_input or []),
            _approvals={} if _approvals is None else _approvals,
            tool_input=tool_input,
        )
        self.tool_name = (
            _assert_must_pass_tool_name() if tool_name is _MISSING else cast(str, tool_name)
        )
        self.tool_arguments = (
            _assert_must_pass_tool_arguments()
            if tool_arguments is _MISSING
            else cast(str, tool_arguments)
        )
        self.tool_call_id = (
            _assert_must_pass_tool_call_id()
            if tool_call_id is _MISSING
            else cast(str, tool_call_id)
        )
        self.tool_call = tool_call
        self.tool_namespace = (
            tool_namespace
            if isinstance(tool_namespace, str)
            else get_tool_call_namespace(tool_call)
        )
        self.agent = agent
        self.run_config = run_config

    @property
    def qualified_tool_name(self) -> str:
        """Return the tool name qualified by namespace when available."""
        return tool_trace_name(self.tool_name, self.tool_namespace) or self.tool_name

    @classmethod
    def from_agent_context(
        cls,
        context: RunContextWrapper[TContext],
        tool_call_id: str,
        tool_call: ResponseFunctionToolCall | None = None,
        agent: AgentBase[Any] | None = None,
        *,
        tool_namespace: str | None = None,
        run_config: RunConfig | None = None,
    ) -> ToolContext:
        """
        Create a ToolContext from a RunContextWrapper.
        """
        # Grab the names of the RunContextWrapper's init=True fields
        base_values: dict[str, Any] = {
            f.name: getattr(context, f.name) for f in fields(RunContextWrapper) if f.init
        }
        tool_name = tool_call.name if tool_call is not None else _assert_must_pass_tool_name()
        tool_args = (
            tool_call.arguments if tool_call is not None else _assert_must_pass_tool_arguments()
        )
        tool_agent = agent
        if tool_agent is None and isinstance(context, ToolContext):
            tool_agent = context.agent
        tool_run_config = run_config
        if tool_run_config is None and isinstance(context, ToolContext):
            tool_run_config = context.run_config

        tool_context = cls(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            tool_arguments=tool_args,
            tool_call=tool_call,
            tool_namespace=(
                tool_namespace
                if isinstance(tool_namespace, str)
                else (
                    getattr(tool_call, "namespace", None)
                    if tool_call is not None
                    and isinstance(getattr(tool_call, "namespace", None), str)
                    else None
                )
            ),
            agent=tool_agent,
            run_config=tool_run_config,
            **base_values,
        )
        set_agent_tool_state_scope(tool_context, get_agent_tool_state_scope(context))
        return tool_context
