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

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from .._tool_identity import get_tool_call_name, get_tool_call_trace_name

if TYPE_CHECKING:
    from ..run_config import CallModelData, ModelInputData

logger = logging.getLogger(__name__)

# Prose keywords worth dropping from a replayed schema: they cost tokens but the model can
# still call the tool without them.
_PROSE_SCHEMA_KEYWORDS = frozenset({"description", "title", "$comment", "examples"})

# Keywords whose value is a map keyed by *user-chosen names* — parameter names, definition
# names, regexes — rather than by schema keywords. Their keys must survive even when they
# spell one of the prose keywords above, so they are recursed into by value only.
_NAME_KEYED_SCHEMA_MAPS = frozenset(
    {
        "properties",
        "patternProperties",
        "$defs",
        "definitions",
        "dependentSchemas",
        "dependentRequired",
    }
)

# Keywords whose value is instance *data* rather than a subschema. Nothing inside them is a
# schema keyword, so they are copied through untouched.
_DATA_SCHEMA_KEYWORDS = frozenset({"default", "const", "enum"})

# Content parts of a structured tool output whose payload is model-readable text. Every other
# part (image, file) carries an opaque payload — a base64 data URL or a file reference — that
# says nothing about what the tool returned when it is sliced by character count.
_TEXT_CONTENT_PART_TYPES = frozenset({"input_text", "output_text", "text"})


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
        trimmable_tools: Optional tool name or set of tool names whose outputs can be trimmed.
            For namespaced tools, both bare names and qualified ``namespace.name`` entries are
            supported. If ``None``, all tool outputs are eligible for trimming. Defaults
            to ``None``.
    """

    recent_turns: int = 2
    max_output_chars: int = 500
    preview_chars: int = 200
    trimmable_tools: str | Iterable[str] | None = field(default=None)

    def __post_init__(self) -> None:
        if self.recent_turns < 1:
            raise ValueError(f"recent_turns must be >= 1, got {self.recent_turns}")
        if self.max_output_chars < 1:
            raise ValueError(f"max_output_chars must be >= 1, got {self.max_output_chars}")
        if self.preview_chars < 0:
            raise ValueError(f"preview_chars must be >= 0, got {self.preview_chars}")
        # Coerce configured tool names to frozenset for immutability.
        if self.trimmable_tools is not None:
            if isinstance(self.trimmable_tools, str):
                trimmable_tools = frozenset({self.trimmable_tools})
            elif isinstance(self.trimmable_tools, bytes):
                raise ValueError("trimmable_tools must be a string or iterable of strings")
            elif isinstance(self.trimmable_tools, frozenset):
                trimmable_tools = self.trimmable_tools
            else:
                trimmable_tools = frozenset(self.trimmable_tools)
            object.__setattr__(self, "trimmable_tools", trimmable_tools)

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

        call_id_to_names = self._build_call_id_to_names(items)

        trimmed_count = 0
        chars_saved = 0
        new_items: list[Any] = []

        for i, item in enumerate(items):
            if i < boundary and isinstance(item, dict):
                item_dict = cast(dict[str, Any], item)
                item_type = item_dict.get("type")
                call_id = str(item_dict.get("call_id") or item_dict.get("id") or "")
                tool_names = call_id_to_names.get(
                    call_id,
                    ("tool_search",) if item_type == "tool_search_output" else (),
                )

                trimmable_tools = cast(frozenset[str] | None, self.trimmable_tools)
                if trimmable_tools is not None and not any(
                    candidate in trimmable_tools for candidate in tool_names
                ):
                    new_items.append(item)
                    continue

                trimmed_item: dict[str, Any] | None = None
                saved_chars = 0
                if item_type == "function_call_output":
                    trimmed_item, saved_chars = self._trim_function_call_output(
                        item_dict, tool_names
                    )
                elif item_type == "tool_search_output":
                    trimmed_item, saved_chars = self._trim_tool_search_output(item_dict)

                if trimmed_item is not None:
                    new_items.append(trimmed_item)
                    trimmed_count += 1
                    chars_saved += saved_chars
                    continue

            new_items.append(item)

        if trimmed_count > 0:
            logger.debug(
                "ToolOutputTrimmer: trimmed %s tool output(s), saved ~%s chars",
                trimmed_count,
                chars_saved,
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

    def _build_call_id_to_names(self, items: list[Any]) -> dict[str, tuple[str, ...]]:
        """Build a mapping from function call_id to candidate tool names."""
        mapping: dict[str, tuple[str, ...]] = {}
        for item in items:
            if isinstance(item, dict) and item.get("type") == "function_call":
                call_id = item.get("call_id")
                qualified_name = get_tool_call_trace_name(item)
                bare_name = get_tool_call_name(item)
                names: list[str] = []
                if qualified_name:
                    names.append(qualified_name)
                if bare_name and bare_name != qualified_name:
                    names.append(bare_name)
                if call_id and names:
                    mapping[str(call_id)] = tuple(names)
            elif isinstance(item, dict) and item.get("type") == "tool_search_call":
                call_id = item.get("call_id") or item.get("id")
                if call_id:
                    mapping[str(call_id)] = ("tool_search",)
        return mapping

    def _trim_function_call_output(
        self,
        item: dict[str, Any],
        tool_names: tuple[str, ...],
    ) -> tuple[dict[str, Any] | None, int]:
        """Trim a function_call_output item when its serialized output is too large."""
        output = item.get("output", "")
        if isinstance(output, list):
            return self._trim_structured_function_call_output(item, output, tool_names)

        output_str = output if isinstance(output, str) else str(output)
        output_len = len(output_str)
        if output_len <= self.max_output_chars:
            return None, 0

        tool_name = tool_names[0] if tool_names else ""
        display_name = tool_name or "unknown_tool"
        preview = output_str[: self.preview_chars]
        summary = (
            f"[Trimmed: {display_name} output — {output_len} chars → "
            f"{self.preview_chars} char preview]\n{preview}..."
        )
        if len(summary) >= output_len:
            return None, 0

        trimmed_item = dict(item)
        trimmed_item["output"] = summary
        return trimmed_item, output_len - len(summary)

    def _trim_structured_function_call_output(
        self,
        item: dict[str, Any],
        parts: list[Any],
        tool_names: tuple[str, ...],
    ) -> tuple[dict[str, Any] | None, int]:
        """Trim a function_call_output that carries structured content parts.

        The SDK emits this shape whenever a tool returns ``ToolOutputText``,
        ``ToolOutputImage`` or ``ToolOutputFileContent``. Only text parts can be previewed
        as characters: slicing the list's serialization instead spends the whole preview
        budget on structural noise, and on a base64 data URL when an image part comes
        first. Image and file parts are therefore dropped and named rather than sliced.

        The trim threshold is left as it is for every other output: the serialized length
        of the whole value.
        """
        output_len = len(str(parts))
        if output_len <= self.max_output_chars:
            return None, 0

        text_segments: list[str] = []
        dropped_part_types: dict[str, int] = {}
        for part in parts:
            # A history item can hold anything a caller once put there, so the part type is
            # only trusted once it is known to be a string: an unhashable value would
            # otherwise raise from the set membership test below.
            raw_part_type = part.get("type") if isinstance(part, dict) else None
            part_type = raw_part_type if isinstance(raw_part_type, str) else ""
            if part_type in _TEXT_CONTENT_PART_TYPES:
                text = cast(dict[str, Any], part).get("text")
                if isinstance(text, str):
                    text_segments.append(text)
                    continue
            dropped_part_types[part_type or "unknown"] = (
                dropped_part_types.get(part_type or "unknown", 0) + 1
            )

        display_name = (tool_names[0] if tool_names else "") or "unknown_tool"
        dropped_note = ""
        if dropped_part_types:
            dropped_note = "; dropped " + ", ".join(
                f"{count} {label}" for label, count in sorted(dropped_part_types.items())
            )
        preview = "\n".join(text_segments)[: self.preview_chars]
        # Only promise a preview when there is text to show; a structured output can carry
        # no text part at all, and then the parts list is the whole message.
        preview_note = f" → {self.preview_chars} char preview" if preview else ""
        summary = (
            f"[Trimmed: {display_name} output — {output_len} chars{preview_note}{dropped_note}]"
        )
        if preview:
            summary = f"{summary}\n{preview}..."
        if len(summary) >= output_len:
            return None, 0

        trimmed_item = dict(item)
        trimmed_item["output"] = summary
        return trimmed_item, output_len - len(summary)

    def _trim_tool_search_output(self, item: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
        """Trim a tool_search_output item while keeping a valid replayable shape."""
        if isinstance(item.get("results"), list):
            return self._trim_legacy_tool_search_results(item)

        tools = item.get("tools")
        if not isinstance(tools, list):
            return None, 0

        original = self._serialize_json_like(tools)
        if len(original) <= self.max_output_chars:
            return None, 0

        trimmed_tools = [self._trim_tool_search_tool(tool) for tool in tools]
        trimmed = self._serialize_json_like(trimmed_tools)
        if len(trimmed) >= len(original):
            return None, 0

        trimmed_item = dict(item)
        trimmed_item["tools"] = trimmed_tools
        return trimmed_item, len(original) - len(trimmed)

    def _trim_legacy_tool_search_results(
        self,
        item: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, int]:
        """Trim legacy partial tool_search_output snapshots that still store free-text results."""
        serialized_results = self._serialize_json_like(item.get("results"))
        output_len = len(serialized_results)
        if output_len <= self.max_output_chars:
            return None, 0

        preview = serialized_results[: self.preview_chars]
        summary = (
            f"[Trimmed: tool_search output — {output_len} chars → "
            f"{self.preview_chars} char preview]\n{preview}..."
        )
        if len(summary) >= output_len:
            return None, 0

        trimmed_item = dict(item)
        trimmed_item["results"] = [{"text": summary}]
        return trimmed_item, output_len - len(summary)

    def _trim_tool_search_tool(self, tool: Any) -> Any:
        """Recursively strip bulky descriptions and schema prose from tool search results."""
        if not isinstance(tool, dict):
            return tool

        trimmed_tool = dict(tool)
        if isinstance(trimmed_tool.get("description"), str):
            trimmed_tool["description"] = trimmed_tool["description"][: self.preview_chars]
            if len(tool["description"]) > self.preview_chars:
                trimmed_tool["description"] += "..."

        tool_type = trimmed_tool.get("type")
        if tool_type == "function" and isinstance(trimmed_tool.get("parameters"), dict):
            trimmed_tool["parameters"] = self._trim_json_schema(trimmed_tool["parameters"])
        elif tool_type == "namespace" and isinstance(trimmed_tool.get("tools"), list):
            trimmed_tool["tools"] = [
                self._trim_tool_search_tool(nested_tool) for nested_tool in trimmed_tool["tools"]
            ]

        return trimmed_tool

    def _trim_json_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        """Remove verbose prose from a JSON schema while preserving its structure."""
        trimmed_schema: dict[str, Any] = {}
        for key, value in schema.items():
            # A name-keyed map is keyed by parameter/definition names, not by schema
            # keywords, so recurse into its values while keeping its keys verbatim.
            # Dropping a key here would delete a declared parameter or dangle a $ref.
            if key in _NAME_KEYED_SCHEMA_MAPS and isinstance(value, dict):
                trimmed_schema[key] = {
                    name: self._trim_json_schema(sub) if isinstance(sub, dict) else sub
                    for name, sub in value.items()
                }
                continue
            # These hold instance data. A "title" key inside a default value is part of the
            # value, so trimming it would silently change the tool's contract.
            if key in _DATA_SCHEMA_KEYWORDS:
                trimmed_schema[key] = value
                continue
            if key in _PROSE_SCHEMA_KEYWORDS:
                continue
            if isinstance(value, dict):
                trimmed_schema[key] = self._trim_json_schema(value)
            elif isinstance(value, list):
                trimmed_schema[key] = [
                    self._trim_json_schema(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                trimmed_schema[key] = value
        return trimmed_schema

    def _serialize_json_like(self, value: Any) -> str:
        """Serialize structured tool output for sizing comparisons."""
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            return str(value)
