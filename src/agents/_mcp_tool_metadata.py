from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MCPToolMetadata:
    """Resolved display metadata for an MCP tool."""

    description: str | None = None
    title: str | None = None


def _get_mapping_or_attr(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _get_non_empty_string(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def resolve_mcp_tool_title(tool: Any) -> str | None:
    """Return the MCP display title, preferring explicit title over annotations.title."""
    explicit_title = _get_non_empty_string(_get_mapping_or_attr(tool, "title"))
    if explicit_title is not None:
        return explicit_title

    annotations = _get_mapping_or_attr(tool, "annotations")
    return _get_non_empty_string(_get_mapping_or_attr(annotations, "title"))


def resolve_mcp_tool_description(tool: Any) -> str | None:
    """Return the MCP tool description when present."""
    return _get_non_empty_string(_get_mapping_or_attr(tool, "description"))


def resolve_mcp_tool_description_for_model(tool: Any) -> str:
    """Return the best model-facing description for an MCP tool.

    MCP distinguishes between a long-form description and a short display title.
    When the description is absent, fall back to the title so local MCP tools do not
    become blank function definitions for the model.
    """

    return resolve_mcp_tool_description(tool) or resolve_mcp_tool_title(tool) or ""


def extract_mcp_tool_metadata(tool: Any) -> MCPToolMetadata:
    """Resolve display metadata from an MCP tool-like object."""
    return MCPToolMetadata(
        description=resolve_mcp_tool_description(tool),
        title=resolve_mcp_tool_title(tool),
    )


def collect_mcp_list_tools_metadata(items: Iterable[Any]) -> dict[tuple[str, str], MCPToolMetadata]:
    """Collect hosted MCP tool metadata from input/output items.

    Accepts raw `mcp_list_tools` payloads, SDK models, or run items whose `raw_item`
    contains an `mcp_list_tools` payload.
    """

    metadata_map: dict[tuple[str, str], MCPToolMetadata] = {}

    for item in items:
        raw_item = _get_mapping_or_attr(item, "raw_item") or item
        if _get_mapping_or_attr(raw_item, "type") != "mcp_list_tools":
            continue

        server_label = _get_non_empty_string(_get_mapping_or_attr(raw_item, "server_label"))
        tools = _get_mapping_or_attr(raw_item, "tools")
        if server_label is None or not isinstance(tools, list):
            continue

        for tool in tools:
            name = _get_non_empty_string(_get_mapping_or_attr(tool, "name"))
            if name is None:
                continue
            metadata_map[(server_label, name)] = extract_mcp_tool_metadata(tool)

    return metadata_map
