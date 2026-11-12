"""Managed child streams and extractors keep their exact native result owner."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agents import (
    Agent,
    NativeAgents,
    NativeRoleTool,
    NativeRoleToolResult,
    NativeRoleToolStreamEvent,
)
from agents.observation import TurnFinished, TurnStarted
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_agent import provider
from tests.test_native_child_agents import collaboration, trust
from tests.test_native_child_declarations import _role
from tests.test_native_executor_tools import declaration
from tests.test_native_profile_model_controls import MODEL as ROLE_MODEL


@pytest.mark.parametrize("restore", [False, True])
async def test_managed_stream_drains_before_extracted_result(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restore: bool
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    events: list[NativeRoleToolStreamEvent] = []
    extracted: list[NativeRoleToolResult] = []
    order: list[str] = []

    async def observe(value: NativeRoleToolStreamEvent) -> None:
        events.append(value)
        if isinstance(value["event"], TurnFinished):
            entered.set()
            await release.wait()
            order.append("stream drained")

    def extract(value: NativeRoleToolResult) -> str:
        extracted.append(value)
        order.append("extracted")
        return "selected child result"

    tool = NativeRoleTool(
        name="research_topic",
        role="research",
        description="Research a topic.",
        on_stream=observe,
        extract_result=extract,
    )
    responses.respond(
        collaboration(tool.name, {"input": "Read the exact record."}, "managed-stream")
    )
    responses.respond(assistant_output("child final result"))
    responses.respond(assistant_output("parent final result"))
    agent = Agent(
        instructions="Use the declared research tool.",
        model=ROLE_MODEL,
        provider=provider(responses),
        tools=[NativeAgents(additional_roles=[_role()], managed_tools=[tool])],
    )
    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        state = None
        if restore:
            async with agent.session(environment=declaration(executor)) as initial:
                state = await initial.save_state()
        async with agent.session(state=state, environment=declaration(executor)) as session:
            root = await session.submit("Research the record.")
            try:
                await entered.wait()
                assert len(responses.requests) == 2
                assert extracted == []
            finally:
                release.set()
            assert (await root.get_result()).final_output == "parent final result"
    assert order == ["stream drained", "extracted"]
    assert isinstance(events[0]["event"], TurnStarted)
    assert isinstance(events[-1]["event"], TurnFinished)
    assert [value["event"].sequence for value in events] == sorted(
        value["event"].sequence for value in events
    )
    origin = events[0]["invocation"]
    assert all(value["invocation"] == origin for value in events)
    assert origin["input"] == "Read the exact record."
    assert origin["native_call_id"] == "managed-stream"
    assert origin["parent_turn_id"] == root.turn_id
    assert origin["tool_name"] == tool.name
    assert origin["child_thread_id"] != origin["parent_thread_id"]
    assert extracted[0]["thread_id"] == origin["child_thread_id"]
    assert extracted[0]["turn_id"] == origin["child_turn_id"]
    assert extracted[0]["final_output"] == "child final result"
    assert "child final result" in json.dumps(extracted[0]["items"])
    assert "selected child result" in json.dumps(responses.requests[-1]["input"])


async def test_managed_extractor_error_returns_a_recoverable_tool_failure(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[NativeRoleToolResult] = []

    def extract(value: NativeRoleToolResult) -> str:
        captured.append(value)
        raise ValueError("private extractor detail")

    tool = NativeRoleTool(
        name="research_topic", role="research", description="Research.", extract_result=extract
    )
    responses.respond(collaboration(tool.name, {"input": "Read the record."}, "managed-error"))
    responses.respond(assistant_output("child result"))
    responses.respond(assistant_output("recovered parent result"))
    responses.respond(assistant_output("following root result"))
    agent = Agent(
        instructions="Use the research tool.",
        model=ROLE_MODEL,
        provider=provider(responses),
        tools=[NativeAgents(additional_roles=[_role()], managed_tools=[tool])],
    )
    async with timeout(30), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        async with agent.session(environment=declaration(executor)) as session:
            assert (
                await (await session.submit("Research.")).get_result()
            ).final_output == "recovered parent result"
            assert (
                await (await session.submit("Continue.")).get_result()
            ).final_output == "following root result"
    assert len(captured) == 1
    wire = json.dumps(responses.requests[2]["input"])
    assert "withheld" in wire
    assert "private extractor detail" not in wire
