"""Unit tests for src/agents/_mcp_tool_metadata.py pure helpers.

The module resolves MCP tool display metadata (title / description) from
either dict payloads or attribute-bearing objects. It feeds hosted-MCP
tool definitions into the model and into traces, but had no direct
test file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agents._mcp_tool_metadata import (
    MCPToolMetadata,
    collect_mcp_list_tools_metadata,
    extract_mcp_tool_metadata,
    resolve_mcp_tool_description,
    resolve_mcp_tool_description_for_model,
    resolve_mcp_tool_title,
)


@dataclass
class _ToolObj:
    """Tiny attribute-bearing stand-in for an MCP tool object."""

    name: str | None = None
    title: str | None = None
    description: str | None = None
    annotations: Any = None


@dataclass
class _Annotations:
    title: str | None = None


class TestResolveMCPToolTitle:
    def test_explicit_title_wins(self) -> None:
        tool = {"title": "Explicit", "annotations": {"title": "Annotated"}}
        assert resolve_mcp_tool_title(tool) == "Explicit"

    def test_falls_back_to_annotations_title(self) -> None:
        tool = {"annotations": {"title": "Annotated"}}
        assert resolve_mcp_tool_title(tool) == "Annotated"

    def test_returns_none_when_neither_present(self) -> None:
        assert resolve_mcp_tool_title({}) is None

    def test_skips_empty_explicit_title(self) -> None:
        tool = {"title": "", "annotations": {"title": "Annotated"}}
        assert resolve_mcp_tool_title(tool) == "Annotated"

    def test_skips_non_string_explicit_title(self) -> None:
        tool = {"title": 123, "annotations": {"title": "Annotated"}}
        assert resolve_mcp_tool_title(tool) == "Annotated"

    def test_works_with_attribute_objects(self) -> None:
        tool = _ToolObj(title="Explicit")
        assert resolve_mcp_tool_title(tool) == "Explicit"

    def test_works_with_attribute_annotations(self) -> None:
        tool = _ToolObj(annotations=_Annotations(title="Annotated"))
        assert resolve_mcp_tool_title(tool) == "Annotated"

    def test_handles_missing_annotations_attribute(self) -> None:
        tool = _ToolObj()
        assert resolve_mcp_tool_title(tool) is None


class TestResolveMCPToolDescription:
    def test_returns_description(self) -> None:
        assert resolve_mcp_tool_description({"description": "Long form"}) == "Long form"

    def test_returns_none_when_empty(self) -> None:
        assert resolve_mcp_tool_description({"description": ""}) is None

    def test_returns_none_when_missing(self) -> None:
        assert resolve_mcp_tool_description({}) is None

    def test_returns_none_when_non_string(self) -> None:
        assert resolve_mcp_tool_description({"description": 123}) is None

    def test_works_with_attribute_object(self) -> None:
        assert resolve_mcp_tool_description(_ToolObj(description="Long form")) == "Long form"


class TestResolveMCPToolDescriptionForModel:
    def test_uses_description_when_present(self) -> None:
        tool = {"description": "Long form", "title": "Short"}
        assert resolve_mcp_tool_description_for_model(tool) == "Long form"

    def test_falls_back_to_title_when_description_missing(self) -> None:
        assert resolve_mcp_tool_description_for_model({"title": "Short"}) == "Short"

    def test_falls_back_to_annotations_title(self) -> None:
        tool = {"annotations": {"title": "Annotated"}}
        assert resolve_mcp_tool_description_for_model(tool) == "Annotated"

    def test_returns_empty_string_when_nothing_resolvable(self) -> None:
        assert resolve_mcp_tool_description_for_model({}) == ""


class TestExtractMCPToolMetadata:
    def test_collects_both_fields(self) -> None:
        tool = {"description": "Long form", "title": "Short"}
        assert extract_mcp_tool_metadata(tool) == MCPToolMetadata(
            description="Long form", title="Short"
        )

    def test_returns_empty_metadata_when_nothing_present(self) -> None:
        assert extract_mcp_tool_metadata({}) == MCPToolMetadata()


class TestCollectMCPListToolsMetadata:
    def test_collects_from_raw_payload(self) -> None:
        items = [
            {
                "type": "mcp_list_tools",
                "server_label": "github",
                "tools": [
                    {"name": "search", "description": "Search repos", "title": "Search"},
                    {"name": "create", "description": "Create issue"},
                ],
            }
        ]
        result = collect_mcp_list_tools_metadata(items)
        assert result == {
            ("github", "search"): MCPToolMetadata(description="Search repos", title="Search"),
            ("github", "create"): MCPToolMetadata(description="Create issue"),
        }

    def test_unwraps_run_item_with_raw_item(self) -> None:
        @dataclass
        class _RunItem:
            raw_item: Any

        run_item = _RunItem(
            raw_item={
                "type": "mcp_list_tools",
                "server_label": "internal",
                "tools": [{"name": "ping", "description": "Ping"}],
            }
        )
        result = collect_mcp_list_tools_metadata([run_item])
        assert result == {("internal", "ping"): MCPToolMetadata(description="Ping")}

    def test_skips_items_without_correct_type(self) -> None:
        items = [
            {
                "type": "mcp_call",
                "server_label": "github",
                "tools": [{"name": "ignored"}],
            }
        ]
        assert collect_mcp_list_tools_metadata(items) == {}

    def test_skips_items_without_server_label(self) -> None:
        items = [
            {
                "type": "mcp_list_tools",
                "tools": [{"name": "search"}],
            }
        ]
        assert collect_mcp_list_tools_metadata(items) == {}

    def test_skips_items_with_non_list_tools(self) -> None:
        items = [
            {
                "type": "mcp_list_tools",
                "server_label": "github",
                "tools": "not-a-list",
            }
        ]
        assert collect_mcp_list_tools_metadata(items) == {}

    def test_skips_tools_without_name(self) -> None:
        items = [
            {
                "type": "mcp_list_tools",
                "server_label": "github",
                "tools": [
                    {"description": "no name"},
                    {"name": "", "description": "empty name"},
                    {"name": "good", "description": "kept"},
                ],
            }
        ]
        result = collect_mcp_list_tools_metadata(items)
        assert result == {("github", "good"): MCPToolMetadata(description="kept")}

    def test_returns_empty_for_empty_input(self) -> None:
        assert collect_mcp_list_tools_metadata([]) == {}

    def test_later_entry_for_same_key_wins(self) -> None:
        items = [
            {
                "type": "mcp_list_tools",
                "server_label": "github",
                "tools": [{"name": "search", "description": "first"}],
            },
            {
                "type": "mcp_list_tools",
                "server_label": "github",
                "tools": [{"name": "search", "description": "second"}],
            },
        ]
        result = collect_mcp_list_tools_metadata(items)
        assert result == {("github", "search"): MCPToolMetadata(description="second")}
