from __future__ import annotations

from agents import Agent
from agents.run_internal.tool_use_tracker import (
    AgentToolUseTracker,
    hydrate_tool_use_tracker,
    serialize_tool_use_tracker,
)


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
