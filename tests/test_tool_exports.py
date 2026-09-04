"""Verify that public tool types are re-exported from the top-level agents package."""

import agents
from agents import WebSearchToolImageSettings
from agents.tool import WebSearchToolImageSettings as ToolWebSearchToolImageSettings


def test_web_search_tool_image_settings_is_exported_at_top_level() -> None:
    assert WebSearchToolImageSettings is ToolWebSearchToolImageSettings
    assert "WebSearchToolImageSettings" in agents.__all__
