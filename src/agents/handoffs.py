from __future__ import annotations

import inspect
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Generic, cast, overload

from pydantic import TypeAdapter
from typing_extensions import TypeAlias, TypeVar

from .exceptions import ModelBehaviorError, UserError
from .items import RunItem, TResponseInputItem
from .run_context import RunContextWrapper, TContext
from .strict_schema import ensure_strict_json_schema
from .tracing.spans import SpanError
from .util import _error_tracing, _json, _transforms

if TYPE_CHECKING:
    from .agent import Agent


# The handoff input type is the type of data passed when the agent is called via a handoff.
THandoffInput = TypeVar("THandoffInput", default=Any)

# Type for the dynamic agent resolver function
AgentResolverFunction = Callable[[RunContextWrapper[Any], str], Awaitable[Agent[Any]]]


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


HandoffInputFilter: TypeAlias = Callable[[HandoffInputData], HandoffInputData]
"""A function that filters the input data passed to the next agent."""


@dataclass
class Handoff(Generic[TContext]):
    """A handoff is when an agent delegates a task to another agent.
    For example, in a customer support scenario you might have a "triage agent" that determines
    which agent should handle the user's request, and sub-agents that specialize in different
    areas like billing, account management, etc.
    """

    tool_name: str
    """The name of the tool that represents the handoff."""

    tool_description: str
    """The description of the tool that represents the handoff."""

    input_json_schema: dict[str, Any]
    """The JSON schema for the handoff input. Can be empty if the handoff does not take an input.
    """

    on_invoke_handoff: Callable[[RunContextWrapper[Any], str], Awaitable[Agent[TContext]]]
    """The function that invokes the handoff. The parameters passed are:
    1. The handoff run context
    2. The arguments from the LLM, as a JSON string. Empty string if input_json_schema is empty.

    Must return an agent.
    """

    agent_name: str
    """The name of the agent that is being handed off to."""

    input_filter: HandoffInputFilter | None = None
    """A function that filters the inputs that are passed to the next agent. By default, the new
    agent sees the entire conversation history. In some cases, you may want to filter inputs e.g.
    to remove older inputs, or remove tools from existing inputs.

    The function will receive the entire conversation history so far, including the input item
    that triggered the handoff and a tool call output item representing the handoff tool's output.

    You are free to modify the input history or new items as you see fit. The next agent that
    runs will receive `handoff_input_data.all_items`.

    IMPORTANT: in streaming mode, we will not stream anything as a result of this function. The
    items generated before will already have been streamed.
    """

    strict_json_schema: bool = True
    """Whether the input JSON schema is in strict mode. We **strongly** recommend setting this to
    True, as it increases the likelihood of correct JSON input.
    """

    def get_transfer_message(self, agent: Agent[Any]) -> str:
        base = f"{{'assistant': '{agent.name}'}}"
        return base

    @classmethod
    def default_tool_name(cls, agent: Agent[Any]) -> str:
        return _transforms.transform_string_function_style(f"transfer_to_{agent.name}")

    @classmethod
    def default_tool_description(cls, agent: Agent[Any]) -> str:
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
    agent_name_for_tool: str | None = None, # New parameter
) -> Handoff[TContext]: ...


@overload
def handoff(
    target_agent_or_resolver: Agent[TContext] | AgentResolverFunction,
    *,
    on_handoff: OnHandoffWithInput[THandoffInput],
    input_type: type[THandoffInput],
    tool_description_override: str | None = None,
    tool_name_override: str | None = None,
    input_filter: Callable[[HandoffInputData], HandoffInputData] | None = None,
    agent_name_for_tool: str | None = None, # New parameter
) -> Handoff[TContext]: ...


@overload
def handoff(
    target_agent_or_resolver: Agent[TContext] | AgentResolverFunction,
    *,
    on_handoff: OnHandoffWithoutInput,
    tool_description_override: str | None = None,
    tool_name_override: str | None = None,
    input_filter: Callable[[HandoffInputData], HandoffInputData] | None = None,
    agent_name_for_tool: str | None = None, # New parameter
) -> Handoff[TContext]: ...


def handoff(
    target_agent_or_resolver: Agent[TContext] | AgentResolverFunction,
    tool_name_override: str | None = None,
    tool_description_override: str | None = None,
    on_handoff: OnHandoffWithInput[THandoffInput] | OnHandoffWithoutInput | None = None,
    input_type: type[THandoffInput] | None = None,
    input_filter: Callable[[HandoffInputData], HandoffInputData] | None = None,
    agent_name_for_tool: str | None = None,
) -> Handoff[TContext]:
    """Create a handoff from an agent or an agent resolver function.

    Args:
        target_agent_or_resolver: The concrete agent instance to handoff to, or an async callable
            (AgentResolverFunction) that dynamically resolves to an agent instance.
            The resolver function receives the RunContextWrapper and the JSON string of arguments
            from the LLM, and must return an `Agent` instance.
        tool_name_override: Optional override for the name of the tool that represents the handoff.
        tool_description_override: Optional override for the description of the tool that
            represents the handoff.
        agent_name_for_tool: Required if `target_agent_or_resolver` is a callable. This name is
            used for generating default tool names and descriptions.
        on_handoff: A function that runs *before* the agent resolver or target agent is determined.
            This function can process the input from the LLM.
        input_type: The type of the input to the `on_handoff` callback. If provided, the input
            will be validated against this type. Only relevant if `on_handoff` takes an input.
        input_filter: A function that filters the inputs that are passed to the next agent.
    """
    # Determine agent_name for tool generation
    current_agent_name_for_tool: str
    if isinstance(target_agent_or_resolver, Agent): # type: ignore[arg-type]
        current_agent_name_for_tool = agent_name_for_tool or target_agent_or_resolver.name
    elif callable(target_agent_or_resolver):
        if not agent_name_for_tool:
            raise UserError(
                "agent_name_for_tool must be provided if target_agent_or_resolver is a callable."
            )
        current_agent_name_for_tool = agent_name_for_tool
    else:
        raise UserError(
            "target_agent_or_resolver must be an Agent instance or a callable resolver function."
        )


    assert (on_handoff and input_type) or not (on_handoff and input_type), (
        "You must provide either both on_handoff and input_type, or neither"
    )
    type_adapter: TypeAdapter[Any] | None
    if input_type is not None:
        assert callable(on_handoff), "on_handoff must be callable"
        sig = inspect.signature(on_handoff)
        if len(sig.parameters) != 2: # context and input
            raise UserError("on_handoff with input_type must take two arguments: context and input")

        type_adapter = TypeAdapter(input_type)
        input_json_schema = type_adapter.json_schema()
    else:
        type_adapter = None
        input_json_schema = {} # No input expected by `on_handoff` or no `on_handoff`
        if on_handoff is not None:
            sig = inspect.signature(on_handoff)
            if len(sig.parameters) != 1: # context only
                raise UserError("on_handoff without input_type must take one argument: context")

    async def _invoke_handoff(
        ctx: RunContextWrapper[Any], input_json_str: str | None = None
    ) -> Agent[Any]:
        # Ensure input_json_str is a string for the resolver, defaulting to empty if None
        actual_input_json_for_resolver = input_json_str or ""

        # First, run the on_handoff callback if provided
        if on_handoff:
            if input_type is not None and type_adapter is not None:
                if input_json_str is None: # Check if on_handoff expected input but got None
                    _error_tracing.attach_error_to_current_span(
                        SpanError(
                            message="on_handoff callback expected non-null input, but got None",
                            data={"details": "input_json_str is None"},
                        )
                    )
                    raise ModelBehaviorError("on_handoff callback expected non-null input, but got None")

                validated_input_for_callback = _json.validate_json(
                    json_str=input_json_str,
                    type_adapter=type_adapter,
                    partial=False,
                )
                input_func = cast(OnHandoffWithInput[THandoffInput], on_handoff)
                if inspect.iscoroutinefunction(input_func):
                    await input_func(ctx, validated_input_for_callback)
                else:
                    input_func(ctx, validated_input_for_callback)
            else: # on_handoff exists but takes no input
                no_input_func = cast(OnHandoffWithoutInput, on_handoff)
                if inspect.iscoroutinefunction(no_input_func):
                    await no_input_func(ctx)
                else:
                    no_input_func(ctx)

        # Now, resolve the agent
        if callable(target_agent_or_resolver):
            # It's an AgentResolverFunction
            resolver_func = cast(AgentResolverFunction, target_agent_or_resolver)
            resolved_agent = await resolver_func(ctx, actual_input_json_for_resolver)
            if not isinstance(resolved_agent, Agent): # type: ignore[arg-type]
                 _error_tracing.attach_error_to_current_span(
                    SpanError(
                        message="Agent resolver function did not return an Agent instance.",
                        data={"resolved_type": type(resolved_agent).__name__},
                    )
                )
                 raise ModelBehaviorError("Agent resolver function must return an Agent instance.")
            return resolved_agent
        else:
            # It's a pre-defined Agent instance
            return cast(Agent[Any], target_agent_or_resolver)


    # Use current_agent_name_for_tool for default tool name and description
    tool_name = tool_name_override or Handoff.default_tool_name_for_agent_like(current_agent_name_for_tool)
    tool_description = tool_description_override or Handoff.default_tool_description_for_agent_like(current_agent_name_for_tool, target_agent_or_resolver if isinstance(target_agent_or_resolver, Agent) else None) # type: ignore

    input_json_schema = ensure_strict_json_schema(input_json_schema)

    return Handoff(
        tool_name=tool_name,
        tool_description=tool_description,
        input_json_schema=input_json_schema,
        on_invoke_handoff=_invoke_handoff, # This now handles dynamic resolution
        input_filter=input_filter,
        agent_name=current_agent_name_for_tool, # Populated based on new logic
    )
