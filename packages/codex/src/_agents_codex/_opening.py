"""Capture Agent declarations before native opening can suspend."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from ._function_tools import Tool
from .agent import (
    Agent,
    ContextT,
    ModelSettings,
    NativeProvider,
    OutputT,
    _copy_model_settings,
)
from .schema import OutputSpec


def _capture_opening_agent(
    agent: Agent[ContextT, OutputT],
    *,
    provider: NativeProvider | None,
    model: str | None,
    model_settings: ModelSettings | None,
    tool_selection: Sequence[Tool[ContextT]] | None,
) -> Agent[ContextT, OutputT]:
    """Detach declaration defaults before native opening can suspend."""
    return Agent[ContextT, OutputT](
        instructions=agent.instructions,
        realtime=agent.realtime,
        name=agent.name,
        tools=agent.tools if tool_selection is None else tool_selection,
        handoffs=agent.handoffs,
        model=agent.model if model is None else model,
        model_settings=cast(
            ModelSettings, {**agent.model_settings, **_copy_model_settings(model_settings)}
        ),
        provider=agent.provider if provider is None else provider,
        decisions=agent.decisions,
        output_type=cast(OutputSpec[OutputT], agent.output_type),
        programmatic_tool_calling=agent.programmatic_tool_calling,
        transform_context=agent.transform_context,
        before_compact=agent.before_compact,
        before_complete=agent.before_complete,
        tool_batch_completion=agent.tool_batch_completion,
        input_guardrails=agent.input_guardrails,
        output_guardrails=agent.output_guardrails,
    )
