"""Built-in call_model_input_filter that trims large tool outputs from older turns.

Agentic applications often accumulate large tool outputs (search results, code execution
output, error analyses) that consume significant tokens but lose relevance as the
conversation progresses. This module provides a configurable filter that surgically trims
bulky tool outputs from older turns while keeping recent turns at full fidelity.

Usage::

    from agents.classic import RunConfig
    from agents.classic.extensions import ToolOutputTrimmer

    config = RunConfig(
        call_model_input_filter=ToolOutputTrimmer(
            recent_turns=2,
            max_output_chars=500,
            preview_chars=200,
            trimmable_tools={"search", "execute_code"},
        ),
    )

The trimmer operates as a sliding window: the last ``recent_turns`` user messages (and
all items after them) are never modified. Older tool outputs that exceed
``max_output_chars`` — and optionally belong to ``trimmable_tools`` — are replaced with a
compact preview.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from _agents_shared.tool_output_trimming import ToolOutputTrimming

from .._tool_identity import get_tool_call_trace_name

if TYPE_CHECKING:
    from ..run_config import CallModelData, ModelInputData

logger = logging.getLogger(__name__)


@dataclass
class ToolOutputTrimmer(ToolOutputTrimming):
    """Configurable filter that trims large tool outputs from older conversation turns.

    This class implements the ``CallModelInputFilter`` protocol and can be passed directly
    to ``RunConfig.call_model_input_filter``. It runs immediately before each model call
    and replaces large tool outputs from older turns with a concise preview, reducing token
    usage without losing the context of what happened.

    Args:
        recent_turns: Number of recent user messages whose surrounding items are never
            trimmed. Defaults to 2.
        max_output_chars: Tool outputs above this character count are candidates for
            trimming. Structured outputs count their model-facing string payloads without
            Python or JSON representation overhead, and their replacements fit within this
            budget. Defaults to 500.
        preview_chars: Maximum number of characters of a string output, or the text parts of
            a structured output, to preserve as a preview when trimming. Structured previews
            may be shorter when needed to fit ``max_output_chars``. Defaults to 200.
        trimmable_tools: Optional tool name or set of tool names whose outputs can be trimmed.
            For namespaced tools, both bare names and qualified ``namespace.name`` entries are
            supported. If ``None``, all tool outputs are eligible for trimming. Defaults
            to ``None``.
    """

    def __call__(self, data: CallModelData[Any]) -> ModelInputData:
        """Filter model input immediately before a Classic request."""
        from ..run_config import ModelInputData as _ModelInputData

        model_data = data.model_data
        items = self._trim_items(
            model_data.input, get_tool_call_trace_name, logger, keep_unchanged_copy=True
        )
        if items is None:
            return model_data
        return _ModelInputData(input=items, instructions=model_data.instructions)
