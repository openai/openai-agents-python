"""Capture Agent declarations before native opening can suspend."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass as dataclass, replace
from typing import Generic, cast

from . import tools as _declaration_types
from ._compiled_dataclasses import dataclass as _sdk_dataclass
from .agent import (
    Agent,
    ContextT,
    ModelSettings,
    NativeProvider,
    OutputT,
    _copy_model_settings,
)
from .completion import _BeforeComplete
from .schema import OutputSpec


@_sdk_dataclass(frozen=True)
class _OpeningContributions(Generic[ContextT]):
    """Selected typed defaults before the canonical Agent capture."""

    tools: Sequence[_declaration_types.Tool[ContextT]] | None
    before_complete: _BeforeComplete[ContextT] | None


def _select_opening_contributions(
    agent: Agent[ContextT, OutputT],
) -> _OpeningContributions[ContextT]:
    """Select defaults without binding callbacks or acquiring resources."""
    return _OpeningContributions(
        tools=agent.tools if getattr(agent, "_hosted_tools_provided", True) else None,
        before_complete=agent.before_complete,
    )


def _capture_opening_agent(
    agent: Agent[ContextT, OutputT],
    *,
    provider: NativeProvider | None,
    model: str | None,
    model_settings: ModelSettings | None,
    tool_selection: Sequence[_declaration_types.Tool[ContextT]] | None,
) -> Agent[ContextT, OutputT]:
    """Detach declaration defaults before native opening can suspend."""
    captured_settings = _copy_model_settings(agent.model_settings)
    overrides = _copy_model_settings(model_settings)
    original_retry = captured_settings.get("retry")
    override_retry = overrides.get("retry")
    if original_retry is not None and override_retry is not None:
        overrides["retry"] = replace(
            original_retry,
            max_retries=override_retry.max_retries
            if override_retry.max_retries is not None
            else original_retry.max_retries,
            backoff=override_retry.backoff
            if override_retry.backoff is not None
            else original_retry.backoff,
            policy=override_retry.policy
            if override_retry.policy is not None
            else original_retry.policy,
        )
    captured_settings.update(overrides)
    contributions = _select_opening_contributions(agent)
    return Agent[ContextT, OutputT](
        instructions=cast(
            str,
            agent.instructions if getattr(agent, "_hosted_instructions_provided", True) else None,
        ),
        hosted_prompt=agent.hosted_prompt,
        realtime=agent.realtime,
        name=agent.name,
        tools=contributions.tools if tool_selection is None else tool_selection,
        handoffs=agent.handoffs,
        model=agent.model if model is None else model,
        model_settings=captured_settings,
        provider=agent.provider if provider is None else provider,
        decisions=agent.decisions,
        output_type=cast(OutputSpec[OutputT], agent.output_type),
        programmatic_tool_calling=agent.programmatic_tool_calling,
        tool_name_collision_policy=agent.tool_name_collision_policy,
        transform_context=agent.transform_context,
        after_model=agent.after_model,
        prepare_handoff_input=agent.prepare_handoff_input,
        before_compact=agent.before_compact,
        compaction=agent.compaction,
        before_complete=contributions.before_complete,
        on_lifecycle=agent.on_lifecycle,
        tool_batch_completion=agent.tool_batch_completion,
        input_guardrails=agent.input_guardrails,
        output_guardrails=agent.output_guardrails,
    )
