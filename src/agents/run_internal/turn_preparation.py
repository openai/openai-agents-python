from __future__ import annotations

import inspect
from typing import Any

from openai.types.responses.response_prompt_param import ResponsePromptParam

from .._redaction import require_clean
from ..agent import Agent
from ..agent_output import AgentOutputSchema, AgentOutputSchemaBase
from ..exceptions import UserError
from ..handoffs import Handoff, handoff
from ..items import TResponseInputItem
from ..lifecycle import AgentHooksBase, RunHooks, RunHooksBase
from ..model_settings import ModelSettings
from ..models.default_models import get_default_model_settings
from ..models.interface import Model
from ..models.multi_provider import MultiProvider
from ..run_config import CallModelData, ModelInputData, ModelSelectionData, RunConfig
from ..run_context import RunContextWrapper, TContext
from ..tool import Tool
from ..tracing import SpanError
from ..util import _error_tracing
from ..util._asyncio_tasks import gather_with_cancel

__all__ = [
    "validate_run_hooks",
    "maybe_filter_model_input",
    "get_output_schema",
    "get_handoffs",
    "get_all_tools",
    "select_model_name",
    "get_model",
    "get_model_settings",
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
        from .._public_agent import get_public_agent
        from ..decisions import _bind_decisions

        async with _bind_decisions(filter_payload, get_public_agent(agent)):
            maybe_updated = run_config.call_model_input_filter(filter_payload)
            updated = await maybe_updated if inspect.isawaitable(maybe_updated) else maybe_updated
        if not isinstance(updated, ModelInputData):
            raise UserError("call_model_input_filter must return a ModelInputData instance")
        return updated
    except Exception as e:
        _error_tracing.attach_error_to_current_span(
            SpanError(
                message="Error in call_model_input_filter",
                data={
                    "error": _error_tracing.get_trace_error(
                        trace_include_sensitive_data=run_config.trace_include_sensitive_data,
                        error_message=str(e),
                    )
                },
            )
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

    results = await gather_with_cancel(*(check_handoff_enabled(h) for h in handoffs))
    enabled: list[Handoff] = [h for h, ok in zip(handoffs, results, strict=False) if ok]
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


async def select_model_name(
    agent: Agent[TContext],
    run_config: RunConfig,
    context_wrapper: RunContextWrapper[TContext],
    turn_number: int,
) -> str | None:
    """Ask only for a new Runner request, never for a retry or approval recovery."""
    selector = run_config.model_selector
    if selector is None or run_config.model is not None or isinstance(agent.model, Model):
        return None
    choice = selector(
        ModelSelectionData(
            agent_name=agent.name,
            model_name=agent.model,
            turn_number=turn_number,
            context=context_wrapper.context,
        )
    )
    name = await choice if inspect.isawaitable(choice) else choice
    if name is not None:
        if not isinstance(name, str) or not name.strip():
            raise UserError("model_selector must return a non-empty model name or None")
        require_clean(name)
    return name


def _effective_model(
    agent: Agent[Any], run_config: RunConfig, selected_model_name: str | None
) -> Model | str | None:
    """Use the same selection for provider lookup and implicit model settings."""
    if run_config.model is not None:
        return run_config.model
    if isinstance(agent.model, Model) or selected_model_name is None:
        return agent.model
    return selected_model_name


def get_model(
    agent: Agent[Any], run_config: RunConfig, *, selected_model_name: str | None = None
) -> Model:
    """Resolve the model instance for this request."""
    selection = _effective_model(agent, run_config, selected_model_name)
    if isinstance(selection, Model):
        return selection
    provider = run_config.model_provider
    if (
        selected_model_name is not None
        and isinstance(provider, MultiProvider)
        and not isinstance(agent.model, Model)
    ):
        provider._validate_model_selection(agent.model, selected_model_name)
    return provider.get_model(selection)


def _model_settings_for_resolved_name(
    agent: Agent[Any], selection: Model | str | None, model: Model
) -> ModelSettings:
    if getattr(model, "_sandbox_model_capabilities", {}).get("use_provider_defaults"):
        return model.get_default_model_settings() or ModelSettings()
    if isinstance(selection, Model):
        return ModelSettings()
    if isinstance(selection, str) and "/" not in selection:
        return get_default_model_settings(selection)
    defaults = model.get_default_model_settings()
    if defaults is not None:
        return defaults
    if isinstance(selection, str):
        return get_default_model_settings(selection)
    return agent.model_settings


def get_model_settings(
    agent: Agent[Any],
    run_config: RunConfig,
    model: Model,
    *,
    selected_model_name: str | None = None,
    prepared_overrides: ModelSettings | None = None,
    prompt: ResponsePromptParam | None = None,
    has_tools: bool = False,
) -> ModelSettings:
    """Resolve model settings, keeping implicit defaults aligned with the resolved model name."""
    settings = agent.model_settings
    if agent._uses_implicit_model_settings():
        selection = _effective_model(agent, run_config, selected_model_name)
        settings = _model_settings_for_resolved_name(agent, selection, model)

    implicit_fields = agent._implicit_request_fields()
    for override in (prepared_overrides, run_config.model_settings):
        if override is not None:
            settings = settings.resolve(override)
            implicit_fields = {name for name in implicit_fields if getattr(override, name) is None}
    implicit_fields = {name for name in implicit_fields if getattr(settings, name) is not None}
    if not implicit_fields:
        return settings
    return model._get_request_settings(
        settings, prompt=prompt, has_tools=has_tools, implicit_fields=implicit_fields
    )
