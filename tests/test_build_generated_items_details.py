"""Tests for build_generated_items_details debug helper."""

from __future__ import annotations

from openai.types.responses import ResponseFunctionToolCall

from agents import Agent
from agents.items import ToolCallItem, ToolCallOutputItem
from agents.run_internal.agent_runner_helpers import build_generated_items_details


def test_extracts_pydantic_tool_call_metadata() -> None:
    """Pydantic raw_items expose name/call_id/type to debug logging."""
    agent = Agent(name="test")
    raw = ResponseFunctionToolCall(
        id="fc_1",
        type="function_call",
        call_id="call_1",
        name="do_thing",
        arguments="{}",
        status="completed",
    )
    items = [ToolCallItem(agent=agent, raw_item=raw)]

    details = build_generated_items_details(items, include_tool_output=True)

    assert len(details) == 1
    entry = details[0]
    assert entry["index"] == 0
    assert entry["type"] == "tool_call_item"
    assert entry["raw_type"] == "function_call"
    assert entry["name"] == "do_thing"
    assert entry["call_id"] == "call_1"


def test_extracts_dict_tool_call_output_with_truncation() -> None:
    """Dict raw_items keep working and tool_call_output_item exposes output."""
    agent = Agent(name="test")
    long_output = "x" * 250
    items = [
        ToolCallOutputItem(
            agent=agent,
            raw_item={
                "type": "function_call_output",
                "call_id": "call_1",
                "output": long_output,
            },
            output=long_output,
        )
    ]

    details = build_generated_items_details(items, include_tool_output=True)

    assert details[0]["raw_type"] == "function_call_output"
    assert details[0]["call_id"] == "call_1"
    assert details[0]["output"] == "x" * 100


def test_omits_output_when_include_tool_output_false() -> None:
    """When include_tool_output=False, output field is not added."""
    agent = Agent(name="test")
    items = [
        ToolCallOutputItem(
            agent=agent,
            raw_item={
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "result",
            },
            output="result",
        )
    ]

    details = build_generated_items_details(items, include_tool_output=False)

    assert "output" not in details[0]
    assert details[0]["call_id"] == "call_1"
