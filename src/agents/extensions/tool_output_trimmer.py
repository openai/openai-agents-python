"""Built-in call_model_input_filter that trims large tool outputs from older turns.

Agentic applications often accumulate large tool outputs (search results, code execution
output, error analyses) that consume significant tokens but lose relevance as the
conversation progresses. This module provides a configurable filter that surgically trims
bulky tool outputs from older turns while keeping recent turns at full fidelity.

Usage::

    from agents import RunConfig
    from agents.extensions import ToolOutputTrimmer

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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..run_config import CallModelData, ModelInputData

logger = logging.getLogger(__name__)


@dataclass
class ToolOutputTrimmer:
    """Configurable filter that trims large tool outputs from older conversation turns.

    This class implements the ``CallModelInputFilter`` protocol and can be passed directly
    to ``RunConfig.call_model_input_filter``. It runs immediately before each model call
    and replaces large tool outputs from older turns with a concise preview, reducing token
    usage without losing the context of what happened.

    Args:
        recent_turns: Number of recent user messages whose surrounding items are never
            trimmed. Defaults to 2.
        max_output_chars: Tool outputs above this character count are candidates for
            trimming. Defaults to 500.
        preview_chars: How many characters of the original output to preserve as a
            preview when trimming. Defaults to 200.
        trimmable_tools: Optional set of tool names whose outputs can be trimmed. If
            ``None``, all tool outputs are eligible for trimming. Defaults to ``None``.
    """

    recent_turns: int = 2
    max_output_chars: int = 500
    preview_chars: int = 200
    trimmable_tools: frozenset[str] | None = field(default=None)

    def __post_init__(self) -> None:
        if self.recent_turns < 1:
            raise ValueError(f"recent_turns must be >= 1, got {self.recent_turns}")
        if self.max_output_chars < 1:
            raise ValueError(f"max_output_chars must be >= 1, got {self.max_output_chars}")
        if self.preview_chars < 0:
            raise ValueError(f"preview_chars must be >= 0, got {self.preview_chars}")
        # Coerce any iterable to frozenset for immutability
        if self.trimmable_tools is not None and not isinstance(self.trimmable_tools, frozenset):
            object.__setattr__(self, "trimmable_tools", frozenset(self.trimmable_tools))

    def __call__(self, data: CallModelData[Any]) -> ModelInputData:
        """Filter callback invoked before each model call.

        Finds the boundary between old and recent items, then trims large tool outputs
        from old turns. Does NOT mutate the original items — creates shallow copies when
        needed.
        """
        from ..run_config import ModelInputData as _ModelInputData

        model_data = data.model_data
        items = model_data.input

        if not items:
            return model_data

        boundary = self._find_recent_boundary(items)
        if boundary == 0:
            return model_data

        call_id_to_name = self._build_call_id_to_name(items)

        trimmed_count = 0
        chars_saved = 0
        new_items: list[Any] = []

        for i, item in enumerate(items):
            if (
                i < boundary
                and isinstance(item, dict)
                and item.get("type") == "function_call_output"
            ):
                output = item.get("output", "")
                output_str = output if isinstance(output, str) else str(output)
                output_len = len(output_str)

                call_id = str(item.get("call_id", ""))
                tool_name = call_id_to_name.get(call_id, "")

                if output_len > self.max_output_chars and (
                    self.trimmable_tools is None or tool_name in self.trimmable_tools
                ):
                    display_name = tool_name or "unknown_tool"
                    preview = output_str[: self.preview_chars]
                    summary = (
                        f"[Trimmed: {display_name} output — {output_len} chars → "
                        f"{self.preview_chars} char preview]\n{preview}..."
                    )
                    # Only replace if summary is actually shorter than the original
                    if len(summary) < output_len:
                        trimmed_item = dict(item)
                        trimmed_item["output"] = summary
                        new_items.append(trimmed_item)

                        trimmed_count += 1
                        chars_saved += output_len - len(summary)
                        continue

            new_items.append(item)

        if trimmed_count > 0:
            logger.debug(
                f"ToolOutputTrimmer: trimmed {trimmed_count} tool output(s), "
                f"saved ~{chars_saved} chars"
            )

        return _ModelInputData(input=new_items, instructions=model_data.instructions)

    def _find_recent_boundary(self, items: list[Any]) -> int:
        """Find the index separating 'old' items from 'recent' items.

        Walks backward through the items list counting user messages. Returns the index
        of the Nth user message from the end, where N = ``recent_turns``. Items at or
        after this index are considered recent and will not be trimmed.

        If there are fewer than N user messages, returns 0 (nothing is old).
        """
        user_msg_count = 0
        for i in range(len(items) - 1, -1, -1):
            item = items[i]
            if isinstance(item, dict) and item.get("role") == "user":
                user_msg_count += 1
                if user_msg_count >= self.recent_turns:
                    return i
        return 0

    def _build_call_id_to_name(self, items: list[Any]) -> dict[str, str]:
        """Build a mapping from function call_id to tool name."""
        mapping: dict[str, str] = {}
        for item in items:
            if isinstance(item, dict) and item.get("type") == "function_call":
                call_id = item.get("call_id")
                name = item.get("name")
                if call_id and name:
                    mapping[call_id] = name
        return mapping
