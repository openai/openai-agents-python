from __future__ import annotations

from typing import Any, cast

from openai.types.responses import ResponseFunctionToolCall

from agents import Agent, ModelSettings, function_tool, tool_namespace
from agents.items import ToolCallItem, ToolCallOutputItem, ToolSearchCallItem, ToolSearchOutputItem
from agents.run_internal.run_loop import maybe_reset_tool_choice
from agents.run_internal.run_steps import ProcessedResponse, ToolRunFunction
from agents.run_internal.tool_use_tracker import (
    AgentToolUseTracker,
    hydrate_tool_use_tracker,
    serialize_tool_use_tracker,
)

from .test_responses import get_function_tool_call


def test_tool_use_tracker_as_serializable_uses_agent_map_or_runtime_snapshot() -> None:
    tracker = AgentToolUseTracker()
    tracker.agent_map = {"agent-a": {"tool-b", "tool-a"}}
    assert tracker.as_serializable() == {"agent-a": ["tool-a", "tool-b"]}

    runtime_tracker = AgentToolUseTracker()
    agent = Agent(name="runtime-agent")
    runtime_tracker.add_tool_use(agent, ["beta", "alpha"])
    assert runtime_tracker.as_serializable() == {"runtime-agent": ["alpha", "beta"]}


def test_tool_use_tracker_from_and_serialize_snapshots() -> None:
    hydrated = AgentToolUseTracker.from_serializable({"agent": ["tool-2", "tool-1"]})
    assert hydrated.agent_map == {"agent": {"tool-1", "tool-2"}}

    runtime_tracker = AgentToolUseTracker()
    agent = Agent(name="serialize-agent")
    runtime_tracker.add_tool_use(agent, ["one"])
    runtime_tracker.add_tool_use(agent, ["two"])
    assert serialize_tool_use_tracker(runtime_tracker) == {"serialize-agent": ["one", "two"]}


def test_serialize_and_hydrate_tool_use_tracker_preserves_duplicate_agent_identity() -> None:
    second = Agent(name="duplicate")
    first = Agent(name="duplicate", handoffs=[second])
    second.handoffs = [first]

    tracker = AgentToolUseTracker()
    tracker.add_tool_use(second, ["approval_tool"])

    snapshot = serialize_tool_use_tracker(tracker, starting_agent=first)
    assert snapshot == {"duplicate#2": ["approval_tool"]}

    class _RunState:
        def get_tool_use_tracker_snapshot(self) -> dict[str, list[str]]:
            return snapshot

    hydrated = AgentToolUseTracker()
    hydrate_tool_use_tracker(
        tool_use_tracker=hydrated,
        run_state=_RunState(),
        starting_agent=first,
    )

    assert hydrated.agent_to_tools == [(second, ["approval_tool"])]


def test_tool_use_tracker_handles_literal_suffix_names_without_collision() -> None:
    literal_suffix = Agent(name="sandbox#2")
    first = Agent(name="sandbox", handoffs=[literal_suffix])
    second = Agent(name="sandbox")
    literal_suffix.handoffs = [first, second]
    first.handoffs = [literal_suffix, second]
    second.handoffs = [first, literal_suffix]

    tracker = AgentToolUseTracker()
    tracker.add_tool_use(second, ["approval_tool"])

    snapshot = serialize_tool_use_tracker(tracker, starting_agent=first)
    assert snapshot == {"sandbox#3": ["approval_tool"]}

    class _RunState:
        def get_tool_use_tracker_snapshot(self) -> dict[str, list[str]]:
            return snapshot

    hydrated = AgentToolUseTracker()
    hydrate_tool_use_tracker(
        tool_use_tracker=hydrated,
        run_state=_RunState(),
        starting_agent=first,
    )

    assert hydrated.agent_to_tools == [(second, ["approval_tool"])]


def test_record_used_tools_uses_trace_names_for_namespaced_and_deferred_functions() -> None:
    agent = Agent(name="tracked-agent")
    tracker = AgentToolUseTracker()

    billing_tool = tool_namespace(
        name="billing",
        description="Billing tools",
        tools=[function_tool(lambda customer_id: customer_id, name_override="lookup_account")],
    )[0]
    deferred_tool = function_tool(
        lambda city: city,
        name_override="get_weather",
        defer_loading=True,
    )

    tracker.record_used_tools(
        agent,
        [
            ToolRunFunction(
                function_tool=billing_tool,
                tool_call=cast(
                    ResponseFunctionToolCall,
                    get_function_tool_call("lookup_account", namespace="billing"),
                ),
            ),
            ToolRunFunction(
                function_tool=deferred_tool,
                tool_call=cast(
                    ResponseFunctionToolCall,
                    get_function_tool_call("get_weather", namespace="get_weather"),
                ),
            ),
        ],
    )

    assert tracker.as_serializable() == {"tracked-agent": ["billing.lookup_account", "get_weather"]}


def test_record_processed_response_ignores_hosted_tool_search_for_resets():
    agent = Agent(name="tracked-agent")
    tracker = AgentToolUseTracker()
    processed_response = ProcessedResponse(
        new_items=[
            ToolSearchCallItem(agent=agent, raw_item={"type": "tool_search_call"}),
            ToolSearchOutputItem(agent=agent, raw_item={"type": "tool_search_output"}),
        ],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=["tool_search", "tool_search"],
        mcp_approval_requests=[],
        interruptions=[],
    )

    tracker.record_processed_response(agent, processed_response)

    assert tracker.has_used_tools(agent) is False
    assert tracker.as_serializable() == {}
    assert maybe_reset_tool_choice(
        agent, tracker, ModelSettings(tool_choice="required")
    ).tool_choice == ("required")


def test_record_processed_response_keeps_function_named_tool_search():
    agent = Agent(name="tracked-agent")
    tracker = AgentToolUseTracker()
    processed_response = ProcessedResponse(
        new_items=[
            ToolSearchCallItem(agent=agent, raw_item={"type": "tool_search_call"}),
            ToolSearchOutputItem(agent=agent, raw_item={"type": "tool_search_output"}),
            ToolCallItem(
                raw_item=cast(ResponseFunctionToolCall, get_function_tool_call("tool_search")),
                agent=agent,
            ),
        ],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=["tool_search", "tool_search", "tool_search"],
        mcp_approval_requests=[],
        interruptions=[],
    )

    tracker.record_processed_response(agent, processed_response)

    assert tracker.as_serializable() == {"tracked-agent": ["tool_search"]}


def test_record_processed_response_counts_output_only_tools_without_shifting_names() -> None:
    agent = Agent(name="tracked-agent")
    tracker = AgentToolUseTracker()
    processed_response = ProcessedResponse(
        new_items=[
            ToolCallOutputItem(
                agent=agent,
                raw_item=cast(
                    Any,
                    {"type": "shell_call_output", "call_id": "shell-1", "output": []},
                ),
                output=[],
            ),
            ToolCallItem(
                raw_item=cast(ResponseFunctionToolCall, get_function_tool_call("lookup_account")),
                agent=agent,
            ),
        ],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=["shell", "lookup_account"],
        mcp_approval_requests=[],
        interruptions=[],
    )

    tracker.record_processed_response(agent, processed_response)

    assert tracker.has_used_tools(agent)
    assert tracker.as_serializable() == {"tracked-agent": ["lookup_account", "shell"]}


def test_hydrate_tool_use_tracker_skips_unknown_agents() -> None:
    class _RunState:
        def get_tool_use_tracker_snapshot(self) -> dict[str, list[str]]:
            return {"known-agent": ["known_tool"], "missing-agent": ["missing_tool"]}

    starting_agent = Agent(name="known-agent")
    tracker = AgentToolUseTracker()

    hydrate_tool_use_tracker(
        tool_use_tracker=tracker,
        run_state=_RunState(),
        starting_agent=starting_agent,
    )

    assert tracker.has_used_tools(starting_agent)
    assert tracker.as_serializable() == {"known-agent": ["known_tool"]}
    assert "missing-agent" not in tracker.as_serializable()
