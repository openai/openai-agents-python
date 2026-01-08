"""Tests for Agent utility properties."""

from __future__ import annotations

from agents.agent import Agent
from agents.tool import function_tool


@function_tool
def sample_tool(x: str) -> str:
    """A sample tool."""
    return x


class TestAgentUtilityProperties:
    """Tests for Agent utility properties."""

    def test_has_tools_true(self) -> None:
        """has_tools should be True when agent has tools."""
        agent = Agent(name="test", tools=[sample_tool])
        assert agent.has_tools is True

    def test_has_tools_false(self) -> None:
        """has_tools should be False when agent has no tools."""
        agent = Agent(name="test")
        assert agent.has_tools is False

    def test_has_handoffs_true(self) -> None:
        """has_handoffs should be True when agent has handoffs."""
        other = Agent(name="other")
        agent = Agent(name="test", handoffs=[other])
        assert agent.has_handoffs is True

    def test_has_handoffs_false(self) -> None:
        """has_handoffs should be False when agent has no handoffs."""
        agent = Agent(name="test")
        assert agent.has_handoffs is False

    def test_tool_names_returns_list(self) -> None:
        """tool_names should return list of tool names."""
        agent = Agent(name="test", tools=[sample_tool])
        assert "sample_tool" in agent.tool_names

    def test_tool_names_empty_list(self) -> None:
        """tool_names should return empty list when no tools."""
        agent = Agent(name="test")
        assert agent.tool_names == []

    def test_handoff_names_returns_list(self) -> None:
        """handoff_names should return list of handoff names."""
        other = Agent(name="handler")
        agent = Agent(name="test", handoffs=[other])
        assert "handler" in agent.handoff_names

    def test_handoff_names_empty_list(self) -> None:
        """handoff_names should return empty list when no handoffs."""
        agent = Agent(name="test")
        assert agent.handoff_names == []

    def test_has_guardrails_false_by_default(self) -> None:
        """has_guardrails should be False by default."""
        agent = Agent(name="test")
        assert agent.has_guardrails is False
