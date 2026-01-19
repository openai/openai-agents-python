from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable
from dataclasses import dataclass, replace as dataclasses_replace
from typing import TYPE_CHECKING, Any, Callable, Generic, cast, overload

from pydantic import TypeAdapter
from typing_extensions import TypeAlias, TypeVar

from ..exceptions import ModelBehaviorError, UserError
from ..items import RunItem, TResponseInputItem
from ..run_context import RunContextWrapper, TContext
from ..strict_schema import ensure_strict_json_schema
from ..tracing.spans import SpanError
from ..util import _error_tracing, _json, _transforms
from ..util._types import MaybeAwaitable
from .history import (
    default_handoff_history_mapper,
    get_conversation_history_wrappers,
    nest_handoff_history,
    reset_conversation_history_wrappers,
    set_conversation_history_wrappers,
)

if TYPE_CHECKING:
    from ..agent import Agent, AgentBase


# The handoff input type is the type of data passed when the agent is called via a handoff.
THandoffInput = TypeVar("THandoffInput", default=Any)

# The agent type that the handoff returns.
TAgent = TypeVar("TAgent", bound="AgentBase[Any]", default="Agent[Any]")

OnHandoffWithInput = Callable[[RunContextWrapper[Any], THandoffInput], Any]
OnHandoffWithoutInput = Callable[[RunContextWrapper[Any]], Any]


@dataclass(frozen=True)
class HandoffInputData:
    input_history: str | tuple[TResponseInputItem, ...]
    """
    The input history before `Runner.run()` was called.
    """

    pre_handoff_items: tuple[RunItem, ...]
    """
    The items generated before the agent turn where the handoff was invoked.
    """

    new_items: tuple[RunItem, ...]
    """
    The new items generated during the current agent turn, including the item that triggered the
    handoff and the tool output message representing the response from the handoff output.
    """

    run_context: RunContextWrapper[Any] | None = None
    """
    The run context at the time the handoff was invoked. Note that, since this property was added
    later on, it is optional for backwards compatibility.
    """

    input_items: tuple[RunItem, ...] | None = None
    """
    Items to include in the next agent's input. When set, these items are used instead of
    new_items for building the input to the next agent. This allows filtering duplicates
    from agent input while preserving all items in new_items for session history.
    """

    def clone(self, **kwargs: Any) -> HandoffInputData:
        """
        Make a copy of the handoff input data, with the given arguments changed. For example, you
        could do:

        ```
        new_handoff_input_data = handoff_input_data.clone(new_items=())
        ```
        """

        return dataclasses_replace(self, **kwargs)


HandoffInputFilter: TypeAlias = Callable[[HandoffInputData], MaybeAwaitable[HandoffInputData]]
"""A function that filters the input data passed to the next agent."""

HandoffHistoryMapper: TypeAlias = Callable[[list[TResponseInputItem]], list[TResponseInputItem]]
"""A function that maps the previous transcript to the nested summary payload."""


@dataclass
class Handoff(Generic[TContext, TAgent]):
    """A handoff is when an agent delegates a task to another agent.

    For example, in a customer support scenario you might have a "triage agent" that determines
    which agent should handle the user's request, and sub-agents that specialize in different areas
    like billing, account management, etc.
    """

    tool_name: str
    """The name of the tool that represents the handoff."""

    tool_description: str
    """The description of the tool that represents the handoff."""

    input_json_schema: dict[str, Any]
    """The JSON schema for the handoff input. Can be empty if the handoff does not take an input."""

    on_invoke_handoff: Callable[[RunContextWrapper[Any], str], Awaitable[TAgent]]
    """The function that invokes the handoff.

    The parameters passed are: (1) the handoff run context, (2) the arguments from the LLM as a
    JSON string (or an empty string if ``input_json_schema`` is empty). Must return an agent.
    """

    agent_name: str
    """The name of the agent that is being handed off to."""

    input_filter: HandoffInputFilter | None = None
    """A function that filters the inputs that are passed to the next agent.

    By default, the new agent sees the entire conversation history. In some cases, you may want to
    filter inputs (for example, to remove older inputs or remove tools from existing inputs). The
    function receives the entire conversation history so far, including the input item that
    triggered the handoff and a tool call output item representing the handoff tool's output. You
    are free to modify the input history or new items as you see fit. The next agent receives the
    input history plus ``input_items`` when provided, otherwise it receives ``new_items``. Use
    ``input_items`` to filter model input while keeping ``new_items`` intact for session history.
    IMPORTANT: in streaming mode, we will not stream anything as a result of this function. The
    items generated before will already have been streamed.
    """

    nest_handoff_history: bool | None = None
    """Override the run-level ``nest_handoff_history`` behavior for this handoff only."""

    strict_json_schema: bool = True
    """Whether the input JSON schema is in strict mode. We strongly recommend setting this to True
    because it increases the likelihood of correct JSON input."""

    is_enabled: bool | Callable[[RunContextWrapper[Any], AgentBase[Any]], MaybeAwaitable[bool]] = (
        True
    )
    """Whether the handoff is enabled.

    Either a bool or a callable that takes the run context and agent and returns whether the
    handoff is enabled. You can use this to dynamically enable or disable a handoff based on your
    context or state.
    """

    def get_transfer_message(self, agent: AgentBase[Any]) -> str:
        return json.dumps({"assistant": agent.name})

    @classmethod
    def default_tool_name(cls, agent: AgentBase[Any]) -> str:
        return _transforms.transform_string_function_style(f"transfer_to_{agent.name}")

    @classmethod
    def default_tool_description(cls, agent: AgentBase[Any]) -> str:
        return (
            f"Handoff to the {agent.name} agent to handle the request. "
            f"{agent.handoff_description or ''}"
        )


@overload
def handoff(
    agent: Agent[TContext],
    *,
    tool_name_override: str | None = None,
    tool_description_override: str | None = None,
    input_filter: Callable[[HandoffInputData], HandoffInputData] | None = None,
    nest_handoff_history: bool | None = None,
    is_enabled: bool | Callable[[RunContextWrapper[Any], Agent[Any]], MaybeAwaitable[bool]] = True,
) -> Handoff[TContext, Agent[TContext]]: ...


@overload
def handoff(
    agent: Agent[TContext],
    *,
    on_handoff: OnHandoffWithInput[THandoffInput],
    input_type: type[THandoffInput],
    tool_description_override: str | None = None,
    tool_name_override: str | None = None,
    input_filter: Callable[[HandoffInputData], HandoffInputData] | None = None,
    nest_handoff_history: bool | None = None,
    is_enabled: bool | Callable[[RunContextWrapper[Any], Agent[Any]], MaybeAwaitable[bool]] = True,
) -> Handoff[TContext, Agent[TContext]]: ...


@overload
def handoff(
    agent: Agent[TContext],
    *,
    on_handoff: OnHandoffWithoutInput,
    tool_description_override: str | None = None,
    tool_name_override: str | None = None,
    input_filter: Callable[[HandoffInputData], HandoffInputData] | None = None,
    nest_handoff_history: bool | None = None,
    is_enabled: bool | Callable[[RunContextWrapper[Any], Agent[Any]], MaybeAwaitable[bool]] = True,
) -> Handoff[TContext, Agent[TContext]]: ...


def handoff(
    agent: Agent[TContext],
    tool_name_override: str | None = None,
    tool_description_override: str | None = None,
    on_handoff: OnHandoffWithInput[THandoffInput] | OnHandoffWithoutInput | None = None,
    input_type: type[THandoffInput] | None = None,
    input_filter: Callable[[HandoffInputData], HandoffInputData] | None = None,
    nest_handoff_history: bool | None = None,
    is_enabled: bool
    | Callable[[RunContextWrapper[Any], Agent[TContext]], MaybeAwaitable[bool]] = True,
) -> Handoff[TContext, Agent[TContext]]:
    """Create a handoff from an agent.

    Args:
        agent: The agent to handoff to.
        tool_name_override: Optional override for the name of the tool that represents the handoff.
        tool_description_override: Optional override for the description of the tool that
            represents the handoff.
        on_handoff: A function that runs when the handoff is invoked.
        input_type: The type of the input to the handoff. If provided, the input will be validated
            against this type. Only relevant if you pass a function that takes an input.
        input_filter: A function that filters the inputs that are passed to the next agent.
        nest_handoff_history: Optional override for the RunConfig-level ``nest_handoff_history``
            flag. If ``None`` we fall back to the run's configuration.
        is_enabled: Whether the handoff is enabled. Can be a bool or a callable that takes the run
            context and agent and returns whether the handoff is enabled. Disabled handoffs are
            hidden from the LLM at runtime.
    """

    assert (on_handoff and input_type) or not (on_handoff and input_type), (
        "You must provide either both on_handoff and input_type, or neither"
    )
    type_adapter: TypeAdapter[Any] | None
    if input_type is not None:
        assert callable(on_handoff), "on_handoff must be callable"
        sig = inspect.signature(on_handoff)
        if len(sig.parameters) != 2:
            raise UserError("on_handoff must take two arguments: context and input")

        type_adapter = TypeAdapter(input_type)
        input_json_schema = type_adapter.json_schema()
    else:
        type_adapter = None
        input_json_schema = {}
        if on_handoff is not None:
            sig = inspect.signature(on_handoff)
            if len(sig.parameters) != 1:
                raise UserError("on_handoff must take one argument: context")

    async def _invoke_handoff(
        ctx: RunContextWrapper[Any], input_json: str | None = None
    ) -> Agent[TContext]:
        if input_type is not None and type_adapter is not None:
            if input_json is None:
                _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Handoff function expected non-null input, but got None",
                        data={"details": "input_json is None"},
                    )
                )
                raise ModelBehaviorError("Handoff function expected non-null input, but got None")

            validated_input = _json.validate_json(
                json_str=input_json,
                type_adapter=type_adapter,
                partial=False,
            )
            input_func = cast(OnHandoffWithInput[THandoffInput], on_handoff)
            if inspect.iscoroutinefunction(input_func):
                await input_func(ctx, validated_input)
            else:
                input_func(ctx, validated_input)
        elif on_handoff is not None:
            no_input_func = cast(OnHandoffWithoutInput, on_handoff)
            if inspect.iscoroutinefunction(no_input_func):
                await no_input_func(ctx)
            else:
                no_input_func(ctx)

        return agent

    tool_name = tool_name_override or Handoff.default_tool_name(agent)
    tool_description = tool_description_override or Handoff.default_tool_description(agent)

    # Always ensure the input JSON schema is in strict mode. If needed, we can make this
    # configurable in the future.
    input_json_schema = ensure_strict_json_schema(input_json_schema)

    async def _is_enabled(ctx: RunContextWrapper[Any], agent_base: AgentBase[Any]) -> bool:
        from ..agent import Agent

        assert callable(is_enabled), "is_enabled must be callable here"
        assert isinstance(agent_base, Agent), "Can't handoff to a non-Agent"
        result = is_enabled(ctx, agent_base)
        if inspect.isawaitable(result):
            return await result
        return bool(result)

    return Handoff(
        tool_name=tool_name,
        tool_description=tool_description,
        input_json_schema=input_json_schema,
        on_invoke_handoff=_invoke_handoff,
        input_filter=input_filter,
        nest_handoff_history=nest_handoff_history,
        agent_name=agent.name,
        is_enabled=_is_enabled if callable(is_enabled) else is_enabled,
    )


__all__ = [
    "Handoff",
    "HandoffHistoryMapper",
    "HandoffInputData",
    "HandoffInputFilter",
    "default_handoff_history_mapper",
    "get_conversation_history_wrappers",
    "handoff",
    "nest_handoff_history",
    "reset_conversation_history_wrappers",
    "set_conversation_history_wrappers",
]
