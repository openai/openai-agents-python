from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable
from dataclasses import dataclass, replace as dataclasses_replace
from typing import TYPE_CHECKING, Any, Callable, Generic, cast, overload

from pydantic import TypeAdapter
from typing_extensions import TypeAlias, TypeVar

from .exceptions import ModelBehaviorError, UserError
from .items import RunItem, TResponseInputItem
from .run_context import RunContextWrapper, TContext
from .strict_schema import ensure_strict_json_schema
from .tracing.spans import SpanError
from .util import _error_tracing, _json, _transforms
from .util._types import MaybeAwaitable

if TYPE_CHECKING:
    from .agent import Agent, AgentBase


# The handoff input type is the type of data passed when the agent is called via a handoff.
THandoffInput = TypeVar("THandoffInput", default=Any)

# The agent type that the handoff returns
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
    The run context at the time the handoff was invoked.
    Note that, since this property was added later on, it's optional for backwards compatibility.
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


@dataclass
class Handoff(Generic[TContext, TAgent]):
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

    on_invoke_handoff: Callable[[RunContextWrapper[Any], str], Awaitable[TAgent]]
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

    is_enabled: bool | Callable[
        [RunContextWrapper[Any], AgentBase[Any]], MaybeAwaitable[bool]
    ] = True
    """Whether the handoff is enabled. Either a bool or a Callable that takes the run context and
    agent and returns whether the handoff is enabled. You can use this to dynamically enable/disable
    a handoff based on your context/state."""

    is_return_to_parent: bool = False
    """Whether this handoff returns control to a parent agent. This enables bidirectional handoff
    workflows where sub-agents can return control to their parent agent.
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
    is_enabled: bool | Callable[[RunContextWrapper[Any], Agent[Any]], MaybeAwaitable[bool]] = True,
) -> Handoff[TContext, Agent[TContext]]: ...


def handoff(
    agent: Agent[TContext],
    tool_name_override: str | None = None,
    tool_description_override: str | None = None,
    on_handoff: OnHandoffWithInput[THandoffInput] | OnHandoffWithoutInput | None = None,
    input_type: type[THandoffInput] | None = None,
    input_filter: Callable[[HandoffInputData], HandoffInputData] | None = None,
    is_enabled: bool
    | Callable[[RunContextWrapper[Any], Agent[TContext]], MaybeAwaitable[bool]] = True,
) -> Handoff[TContext, Agent[TContext]]:
    """Create a handoff from an agent.

    Args:
        agent: The agent to handoff to, or a function that returns an agent.
        tool_name_override: Optional override for the name of the tool that represents the handoff.
        tool_description_override: Optional override for the description of the tool that
            represents the handoff.
        on_handoff: A function that runs when the handoff is invoked.
        input_type: the type of the input to the handoff. If provided, the input will be validated
            against this type. Only relevant if you pass a function that takes an input.
        input_filter: a function that filters the inputs that are passed to the next agent.
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

    # Always ensure the input JSON schema is in strict mode
    # If there is a need, we can make this configurable in the future
    input_json_schema = ensure_strict_json_schema(input_json_schema)

    async def _is_enabled(ctx: RunContextWrapper[Any], agent_base: AgentBase[Any]) -> bool:
        from .agent import Agent

        assert callable(is_enabled), "is_enabled must be callable here"
        assert isinstance(agent_base, Agent), "Can't handoff to a non-Agent"
        result = is_enabled(ctx, agent_base)

        if inspect.isawaitable(result):
            return await result

        return result

    return Handoff(
        tool_name=tool_name,
        tool_description=tool_description,
        input_json_schema=input_json_schema,
        on_invoke_handoff=_invoke_handoff,
        input_filter=input_filter,
        agent_name=agent.name,
        is_enabled=_is_enabled if callable(is_enabled) else is_enabled,
        is_return_to_parent=False,
    )


def return_to_parent_handoff(
    parent_agent: Agent[Any],
    *,
    tool_name_override: str | None = None,
    tool_description_override: str | None = None,
    input_filter: Callable[[HandoffInputData], HandoffInputData] | None = None,
    is_enabled: bool | Callable[[RunContextWrapper[Any], Agent[Any]], MaybeAwaitable[bool]] = True,
) -> Handoff[TContext, Agent[TContext]]:
    """Create a handoff that returns control to the parent agent.
    
    This enables bidirectional handoff workflows where sub-agents can return control to their
    parent agent, allowing for orchestrator-like patterns where a parent agent coordinates
    multiple sub-agents.
    
    Args:
        parent_agent: The parent agent to return control to.
        tool_name_override: Optional override for the name of the tool that represents the handoff.
        tool_description_override: Optional override for the description of the tool that
            represents the handoff.
        input_filter: A function that filters the inputs that are passed to the parent agent.
        is_enabled: Whether the handoff is enabled. Can be a bool or a callable that takes the run
            context and agent and returns whether the handoff is enabled.
    
    Returns:
        A Handoff object that returns control to the parent agent.
    
    Example:
        ```python
        # Create a financial agent that can return to its parent
        financial_agent = Agent(
            name="FinancialAgent",
            instructions="Fetch and analyze financial data",
            handoffs=[
                return_to_parent_handoff(parent_agent)
            ]
        )
        
        # The financial agent can now return control to its parent
        # after completing its task, allowing the parent to coordinate
        # with other agents like a Google Docs agent.
        ```
    """
    tool_name = tool_name_override or "return_to_parent"
    tool_description = tool_description_override or (
        f"Return control to the parent agent ({parent_agent.name}) to continue the workflow. "
        "Use this when you have completed your task and want the parent agent to handle "
        "the next steps in the workflow."
    )

    async def _invoke_return_to_parent(
        ctx: RunContextWrapper[Any], input_json: str | None = None
    ) -> Agent[Any]:
        # Set the parent reference for the current agent if it's not already set
        current_agent = ctx.agent if hasattr(ctx, 'agent') else None
        if current_agent and hasattr(current_agent, 'set_parent'):
            current_agent.set_parent(parent_agent)
        
        return parent_agent

    return Handoff(
        tool_name=tool_name,
        tool_description=tool_description,
        input_json_schema={},  # No input schema for return to parent
        on_invoke_handoff=_invoke_return_to_parent,
        input_filter=input_filter,
        agent_name=parent_agent.name,
        is_enabled=is_enabled,
        is_return_to_parent=True,
    )


def create_bidirectional_handoff_workflow(
    orchestrator_agent: Agent[Any],
    sub_agents: list[Agent[Any]],
    *,
    enable_return_to_parent: bool = True,
) -> tuple[Agent[Any], list[Agent[Any]]]:
    """Create a bidirectional handoff workflow with an orchestrator and sub-agents.
    
    This function sets up a workflow where:
    1. The orchestrator agent can hand off to any sub-agent
    2. Each sub-agent can return control to the orchestrator
    3. The orchestrator can then hand off to other sub-agents
    
    Args:
        orchestrator_agent: The main orchestrator agent that coordinates the workflow.
        sub_agents: List of sub-agents that can be called by the orchestrator.
        enable_return_to_parent: Whether to enable return-to-parent functionality for sub-agents.
    
    Returns:
        A tuple of (orchestrator_agent, sub_agents) with bidirectional handoffs configured.
    
    Example:
        ```python
        # Create agents
        orchestrator = Agent(name="Orchestrator", instructions="Coordinate workflows")
        financial_agent = Agent(name="FinancialAgent", instructions="Fetch financial data")
        docs_agent = Agent(name="DocsAgent", instructions="Handle document operations")
        
        # Set up bidirectional workflow
        orchestrator, sub_agents = create_bidirectional_handoff_workflow(
            orchestrator_agent=orchestrator,
            sub_agents=[financial_agent, docs_agent]
        )
        
        # Now the orchestrator can hand off to financial_agent, which can return
        # to orchestrator, which can then hand off to docs_agent
        ```
    """
    # Set up handoffs from orchestrator to sub-agents
    orchestrator_handoffs = []
    for sub_agent in sub_agents:
        # Set parent reference for sub-agent
        if enable_return_to_parent:
            sub_agent.set_parent(orchestrator_agent)
        
        # Add handoff from orchestrator to sub-agent
        orchestrator_handoffs.append(handoff(sub_agent))
    
    orchestrator_agent.handoffs.extend(orchestrator_handoffs)
    
    # Set up return-to-parent handoffs for sub-agents
    if enable_return_to_parent:
        for sub_agent in sub_agents:
            return_handoff = return_to_parent_handoff(orchestrator_agent)
            sub_agent.handoffs.append(return_handoff)
    
    return orchestrator_agent, sub_agents