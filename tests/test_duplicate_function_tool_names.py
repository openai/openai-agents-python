"""Tests for duplicate plain function-tool name detection (issue #4116).

validate_function_tool_lookup_configuration() previously contained a
``continue`` on the bare/bare collision branch, silently ignoring the most
common case of two plain @function_tool decorators sharing the same name.

The fix replaces that ``continue`` with a UserError so the SDK surfaces the
problem at tool-resolution time instead of letting the OpenAI API reject the
request with an opaque 400.
"""
from __future__ import annotations

import pytest

from agents import Agent, function_tool
from agents._tool_identity import validate_function_tool_lookup_configuration
from agents.exceptions import UserError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@function_tool(name_override="lookup")
def lookup_customers(x: str) -> str:
    """Look up customers."""
    return "a"


@function_tool(name_override="lookup")
def lookup_orders(x: str) -> str:
    """Look up orders."""
    return "b"


@function_tool(name_override="search")
def search_a(x: str) -> str:
    """Search A."""
    return "a"


@function_tool(name_override="search")
def search_b(x: str) -> str:
    """Search B."""
    return "b"


@function_tool
def unique_tool(x: str) -> str:
    """A uniquely named tool."""
    return "ok"


# ---------------------------------------------------------------------------
# validate_function_tool_lookup_configuration direct tests
# ---------------------------------------------------------------------------


class TestValidateFunctionToolLookupConfiguration:
    def test_duplicate_plain_tools_raises(self) -> None:
        """Two bare FunctionTools with the same name must raise UserError."""
        with pytest.raises(UserError, match="lookup"):
            validate_function_tool_lookup_configuration([lookup_customers, lookup_orders])

    def test_error_message_mentions_name_override(self) -> None:
        """The error message should point to name_override= as the fix."""
        with pytest.raises(UserError) as exc_info:
            validate_function_tool_lookup_configuration([lookup_customers, lookup_orders])
        assert "name_override" in str(exc_info.value)

    def test_error_message_includes_tool_name(self) -> None:
        """The error should name the colliding tool name."""
        with pytest.raises(UserError) as exc_info:
            validate_function_tool_lookup_configuration([lookup_customers, lookup_orders])
        assert "lookup" in str(exc_info.value)

    def test_different_duplicate_names_raise(self) -> None:
        """Collision detection works regardless of the specific duplicate name."""
        with pytest.raises(UserError, match="search"):
            validate_function_tool_lookup_configuration([search_a, search_b])

    def test_single_tool_does_not_raise(self) -> None:
        """A single tool can never collide."""
        validate_function_tool_lookup_configuration([lookup_customers])  # no raise

    def test_unique_tools_do_not_raise(self) -> None:
        """Distinct tool names are always valid."""
        validate_function_tool_lookup_configuration([lookup_customers, unique_tool])  # no raise

    def test_three_tools_two_collide_raises(self) -> None:
        """Even when a third unique tool is present, a collision in the first two raises."""
        with pytest.raises(UserError, match="lookup"):
            validate_function_tool_lookup_configuration(
                [lookup_customers, lookup_orders, unique_tool]
            )

    def test_empty_list_does_not_raise(self) -> None:
        """An empty tool list is always valid."""
        validate_function_tool_lookup_configuration([])  # no raise

    def test_same_tool_object_added_twice_raises(self) -> None:
        """Adding the exact same tool object twice also collides on its name."""
        with pytest.raises(UserError, match="lookup"):
            validate_function_tool_lookup_configuration([lookup_customers, lookup_customers])


# ---------------------------------------------------------------------------
# Agent-level integration: collision detected at get_all_tools() runtime
# ---------------------------------------------------------------------------


class TestAgentDuplicateToolDetection:
    @pytest.mark.asyncio
    async def test_agent_with_duplicate_tools_raises_at_runtime(self) -> None:
        """Agent.get_all_tools() must surface the UserError from the lookup map builder."""
        from agents.run_context import RunContextWrapper

        ctx = RunContextWrapper(context=None)
        agent = Agent(
            name="A",
            tools=[lookup_customers, lookup_orders],
        )
        # The collision is not caught at Agent.__post_init__ (tools are arbitrary callables),
        # but must be caught at get_all_tools() time via build_function_tool_lookup_map.
        with pytest.raises(UserError, match="lookup"):
            await agent.get_all_tools(ctx)

    @pytest.mark.asyncio
    async def test_agent_with_unique_tools_does_not_raise(self) -> None:
        """An agent with uniquely-named tools passes validation cleanly."""
        from agents.run_context import RunContextWrapper

        ctx = RunContextWrapper(context=None)
        agent = Agent(
            name="A",
            tools=[lookup_customers, unique_tool],
        )
        tools = await agent.get_all_tools(ctx)
        names = {t.name for t in tools}
        assert "lookup" in names
        assert "unique_tool" in names
