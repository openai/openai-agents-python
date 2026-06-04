"""Verify that user-facing helper types are re-exported from the top-level agents package."""

import agents
from agents import (
    model_settings as model_settings_module,
    tool_context as tool_context_module,
    usage as usage_module,
)


def test_tool_context_is_exported_at_top_level() -> None:
    # ToolContext is the documented context type for function tools and lifecycle hooks,
    # so it must be importable from `agents` like RunContextWrapper.
    from agents import ToolContext

    assert ToolContext is tool_context_module.ToolContext
    assert "ToolContext" in agents.__all__


def test_mcp_tool_choice_is_exported_at_top_level() -> None:
    # MCPToolChoice is the value users assign to ModelSettings.tool_choice to force a
    # specific hosted MCP tool, so it must be importable alongside ModelSettings.
    from agents import MCPToolChoice

    assert MCPToolChoice is model_settings_module.MCPToolChoice
    assert "MCPToolChoice" in agents.__all__


def test_request_usage_is_exported_at_top_level() -> None:
    # RequestUsage is the element type of Usage.request_usage_entries, so users need it
    # importable from `agents` to annotate per-request usage data.
    from agents import RequestUsage

    assert RequestUsage is usage_module.RequestUsage
    assert "RequestUsage" in agents.__all__


def test_exported_names_resolve_to_attributes() -> None:
    # Every name listed in agents.__all__ should resolve via getattr so that
    # `from agents import <name>` works for the package's whole public surface.
    for name in agents.__all__:
        assert hasattr(agents, name), f"agents.{name} is in __all__ but not importable"
