from __future__ import annotations

from agents import Agent, RunContextWrapper

from .utils.factories import make_tool_approval_item


def test_latest_approval_decision_wins_for_call_id() -> None:
    agent = Agent(name="test-agent")
    context_wrapper = RunContextWrapper(context=None)
    approval_item = make_tool_approval_item(agent, call_id="call-1", name="test_tool")

    context_wrapper.approve_tool(approval_item)
    assert context_wrapper.is_tool_approved("test_tool", "call-1") is True

    context_wrapper.reject_tool(approval_item)
    assert context_wrapper.is_tool_approved("test_tool", "call-1") is False

    context_wrapper.approve_tool(approval_item)
    assert context_wrapper.is_tool_approved("test_tool", "call-1") is True
