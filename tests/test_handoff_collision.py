"""Tests for agent-name-normalization collision detection (issue #4118).

Covers:
- Handoff list name collisions detected at Agent construction time.
- as_tool() name collisions detected at runtime via get_all_tools().
- Case-only collisions (the previously silent case).
- Happy path: explicit tool_name_override= avoids the error.
"""
from __future__ import annotations

import pytest

from agents import Agent, handoff
from agents.exceptions import UserError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(name: str) -> Agent:
    """Return a bare agent with the given name."""
    return Agent(name=name)


# ---------------------------------------------------------------------------
# Handoff-list collision tests (caught at Agent.__post_init__)
# ---------------------------------------------------------------------------


class TestHandoffCollisionAtConstruction:
    def test_plain_agents_with_same_derived_name_raises(self) -> None:
        billing = _make_agent("Billing Agent")
        billing_dup = _make_agent("billing agent")
        with pytest.raises(UserError, match="transfer_to_billing_agent"):
            Agent(name="Triage", handoffs=[billing, billing_dup])

    def test_case_only_collision_raises(self) -> None:
        a = _make_agent("Refund")
        b = _make_agent("refund")
        with pytest.raises(UserError, match="transfer_to_refund"):
            Agent(name="Triage", handoffs=[a, b])

    def test_punctuation_collision_raises(self) -> None:
        a = _make_agent("Billing-Agent")
        b = _make_agent("Billing Agent")
        with pytest.raises(UserError, match="transfer_to_billing_agent"):
            Agent(name="Triage", handoffs=[a, b])

    def test_error_message_names_both_agents(self) -> None:
        a = _make_agent("Billing Agent")
        b = _make_agent("billing agent")
        with pytest.raises(UserError) as exc_info:
            Agent(name="Triage", handoffs=[a, b])
        msg = str(exc_info.value)
        assert "Billing Agent" in msg
        assert "billing agent" in msg
        assert "tool_name_override" in msg

    def test_handoff_objects_with_same_derived_name_raises(self) -> None:
        a = _make_agent("Support Agent")
        b = _make_agent("support agent")
        with pytest.raises(UserError, match="transfer_to_support_agent"):
            Agent(name="Triage", handoffs=[handoff(a), handoff(b)])

    def test_mixed_agent_and_handoff_collision_raises(self) -> None:
        a = _make_agent("Support Agent")
        b = _make_agent("support agent")
        with pytest.raises(UserError, match="transfer_to_support_agent"):
            Agent(name="Triage", handoffs=[a, handoff(b)])

    def test_no_collision_with_distinct_names(self) -> None:
        billing = _make_agent("Billing Agent")
        support = _make_agent("Support Agent")
        triage = Agent(name="Triage", handoffs=[billing, support])
        assert len(triage.handoffs) == 2

    def test_no_collision_single_handoff(self) -> None:
        billing = _make_agent("Billing Agent")
        triage = Agent(name="Triage", handoffs=[billing])
        assert len(triage.handoffs) == 1

    def test_explicit_override_avoids_collision(self) -> None:
        a = _make_agent("Billing Agent")
        b = _make_agent("billing agent")
        triage = Agent(
            name="Triage",
            handoffs=[
                a,
                handoff(b, tool_name_override="transfer_to_billing_agent_v2"),
            ],
        )
        assert len(triage.handoffs) == 2

    def test_explicit_override_that_still_collides_raises(self) -> None:
        a = _make_agent("Billing Agent")
        b = _make_agent("billing agent")
        with pytest.raises(UserError, match="transfer_to_billing_agent"):
            Agent(
                name="Triage",
                handoffs=[
                    handoff(a, tool_name_override="transfer_to_billing_agent"),
                    handoff(b, tool_name_override="transfer_to_billing_agent"),
                ],
            )

    def test_empty_handoffs_does_not_raise(self) -> None:
        triage = Agent(name="Triage", handoffs=[])
        assert triage.handoffs == []


# ---------------------------------------------------------------------------
# as_tool() collision tests (caught at get_all_tools() runtime)
# ---------------------------------------------------------------------------


class TestAsToolCollisionAtRuntime:
    @pytest.mark.asyncio
    async def test_as_tool_case_collision_raises(self) -> None:
        from agents.run_context import RunContextWrapper
        ctx = RunContextWrapper(context=None)
        a = _make_agent("Refund")
        b = _make_agent("refund")
        parent = Agent(
            name="Orchestrator",
            tools=[
                a.as_tool(tool_name=None, tool_description="Handles refunds"),
                b.as_tool(tool_name=None, tool_description="Also handles refunds"),
            ],
        )
        with pytest.raises(UserError, match="refund"):
            await parent.get_all_tools(ctx)

    @pytest.mark.asyncio
    async def test_as_tool_punctuation_collision_raises(self) -> None:
        from agents.run_context import RunContextWrapper
        ctx = RunContextWrapper(context=None)
        a = _make_agent("My-Tool")
        b = _make_agent("My Tool")
        parent = Agent(
            name="Orchestrator",
            tools=[
                a.as_tool(tool_name=None, tool_description="desc"),
                b.as_tool(tool_name=None, tool_description="desc"),
            ],
        )
        with pytest.raises(UserError, match="my_tool"):
            await parent.get_all_tools(ctx)

    @pytest.mark.asyncio
    async def test_as_tool_explicit_name_avoids_collision(self) -> None:
        from agents.run_context import RunContextWrapper
        ctx = RunContextWrapper(context=None)
        a = _make_agent("Refund")
        b = _make_agent("refund")
        parent = Agent(
            name="Orchestrator",
            tools=[
                a.as_tool(tool_name="refund_v1", tool_description="desc"),
                b.as_tool(tool_name="refund_v2", tool_description="desc"),
            ],
        )
        tools = await parent.get_all_tools(ctx)
        names = {t.name for t in tools}
        assert "refund_v1" in names
        assert "refund_v2" in names
