from __future__ import annotations

import asyncio
import inspect
from typing import Any

from ..agent import Agent
from ..agent_output import AgentOutputSchema, AgentOutputSchemaBase
from ..exceptions import UserError
from ..handoffs import Handoff, handoff
from ..items import TResponseInputItem
from ..lifecycle import AgentHooksBase, RunHooks, RunHooksBase
from ..models.interface import Model
from ..run_config import CallModelData, ModelInputData, RunConfig
from ..run_context import RunContextWrapper, TContext
from ..tool import Tool
from ..tracing import SpanError
from ..util import _error_tracing

__all__ = [
    "validate_run_hooks",
    "maybe_filter_model_input",
    "get_output_schema",
    "get_handoffs",
    "get_all_tools",
    "get_model",
]


def validate_run_hooks(
    hooks: RunHooksBase[Any, Agent[Any]] | AgentHooksBase[Any, Agent[Any]] | Any | None,
) -> RunHooks[Any]:
    """Normalize hooks input and enforce RunHooks type."""
    if hooks is None:
        return RunHooks[Any]()
    input_hook_type = type(hooks).__name__
    if isinstance(hooks, AgentHooksBase):
        raise TypeError(
            "Run hooks must be instances of RunHooks. "
            f"Received agent-scoped hooks ({input_hook_type}). "
            "Attach AgentHooks to an Agent via Agent(..., hooks=...)."
        )
    if not isinstance(hooks, RunHooksBase):
        raise TypeError(f"Run hooks must be instances of RunHooks. Received {input_hook_type}.")
    return hooks


async def maybe_filter_model_input(
    *,
    agent: Agent[TContext],
    run_config: RunConfig,
    context_wrapper: RunContextWrapper[TContext],
    input_items: list[TResponseInputItem],
    system_instructions: str | None,
) -> ModelInputData:
    """Apply optional call_model_input_filter to modify model input."""
    effective_instructions = system_instructions
    effective_input: list[TResponseInputItem] = input_items

    if run_config.call_model_input_filter is None:
        return ModelInputData(input=effective_input, instructions=effective_instructions)

    try:
        model_input = ModelInputData(
            input=effective_input.copy(),
            instructions=effective_instructions,
        )
        filter_payload: CallModelData[TContext] = CallModelData(
            model_data=model_input,
            agent=agent,
            context=context_wrapper.context,
        )
        maybe_updated = run_config.call_model_input_filter(filter_payload)
        updated = await maybe_updated if inspect.isawaitable(maybe_updated) else maybe_updated
        if not isinstance(updated, ModelInputData):
            raise UserError("call_model_input_filter must return a ModelInputData instance")
        return updated
    except Exception as e:
        _error_tracing.attach_error_to_current_span(
            SpanError(message="Error in call_model_input_filter", data={"error": str(e)})
        )
        raise


async def get_handoffs(agent: Agent[Any], context_wrapper: RunContextWrapper[Any]) -> list[Handoff]:
    """Return enabled handoffs for the agent."""
    handoffs = []
    for handoff_item in agent.handoffs:
        if isinstance(handoff_item, Handoff):
            handoffs.append(handoff_item)
        elif isinstance(handoff_item, Agent):
            handoffs.append(handoff(handoff_item))

    async def check_handoff_enabled(handoff_obj: Handoff) -> bool:
        attr = handoff_obj.is_enabled
        if isinstance(attr, bool):
            return attr
        res = attr(context_wrapper, agent)
        if inspect.isawaitable(res):
            return bool(await res)
        return bool(res)

    results = await asyncio.gather(*(check_handoff_enabled(h) for h in handoffs))
    enabled: list[Handoff] = [h for h, ok in zip(handoffs, results) if ok]
    return enabled


async def get_all_tools(agent: Agent[Any], context_wrapper: RunContextWrapper[Any]) -> list[Tool]:
    """Fetch all tools available to the agent."""
    return await agent.get_all_tools(context_wrapper)


def get_output_schema(agent: Agent[Any]) -> AgentOutputSchemaBase | None:
    """Return the resolved output schema for the agent, if any."""
    if agent.output_type is None or agent.output_type is str:
        return None
    elif isinstance(agent.output_type, AgentOutputSchemaBase):
        return agent.output_type

    return AgentOutputSchema(agent.output_type)


def get_model(agent: Agent[Any], run_config: RunConfig) -> Model:
    """Resolve the model instance for this run."""
    if isinstance(run_config.model, Model):
        return run_config.model
    elif isinstance(run_config.model, str):
        return run_config.model_provider.get_model(run_config.model)
    elif isinstance(agent.model, Model):
        return agent.model

    return run_config.model_provider.get_model(agent.model)
