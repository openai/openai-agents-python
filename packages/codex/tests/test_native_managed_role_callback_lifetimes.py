from __future__ import annotations

import asyncio
import threading

import pytest

from agents import Agent, NativeAgents, NativeRoleTool, ToolOutput, function_tool
from agents.observation import TurnFinished
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider
from tests.test_native_child_agents import collaboration, trust
from tests.test_native_child_declarations import _role
from tests.test_native_executor_tools import declaration
from tests.test_native_profile_model_controls import MODEL as ROLE_MODEL


@pytest.mark.parametrize("kind", ["stream", "extract"])
async def test_sync_managed_callbacks_use_owned_threads(responses, tmp_path, monkeypatch, kind):
    loop = asyncio.get_running_loop()
    main_thread = threading.get_ident()
    entered = asyncio.Event()
    released = threading.Event()
    threads = []
    timers = []

    def callback(value):
        threads.append(threading.get_ident())
        if not released.is_set():
            timer = threading.Timer(0.3, released.set)
            timers.append(timer)
            timer.start()
            loop.call_soon_threadsafe(entered.set)
            released.wait(2)
        return "selected result" if kind == "extract" else None

    tool = NativeRoleTool(
        name="research_topic",
        role="research",
        description="Research.",
        **({"on_stream": callback} if kind == "stream" else {"extract_result": callback}),
    )
    responses.respond(collaboration(tool.name, {"input": "Read."}, "sync-managed"))
    responses.respond(assistant_output("child result"))
    responses.respond(assistant_output("parent result"))
    agent = Agent(
        instructions="Use the research tool.",
        model=ROLE_MODEL,
        provider=provider(responses),
        tools=[NativeAgents(additional_roles=[_role()], managed_tools=[tool])],
    )
    try:
        async with timeout(15), ExecutorPeer(tmp_path / "remote").serve() as peer:
            trust(peer, tmp_path, monkeypatch)
            async with agent.session(environment=declaration(peer)) as session:
                root = await session.submit("Research.")
                await entered.wait()
                assert (await root.get_result()).final_output == "parent result"
    finally:
        released.set()
        for timer in timers:
            timer.join()
    assert threads and all(value != main_thread for value in threads)


async def test_cancelled_owner_drops_the_selected_stream_backlog(responses, tmp_path, monkeypatch):
    entered = asyncio.Event()
    received = []

    async def observe(value):
        received.append(value["event"].sequence)
        if len(received) == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass

    tool = NativeRoleTool(
        name="research_topic", role="research", description="Research.", on_stream=observe
    )
    responses.respond(collaboration(tool.name, {"input": "Read."}, "cancel-managed"))
    responses.respond(assistant_output("child result"))
    responses.respond(assistant_output("following root"))
    agent = Agent(
        instructions="Use the research tool.",
        model=ROLE_MODEL,
        provider=provider(responses),
        tools=[NativeAgents(additional_roles=[_role()], managed_tools=[tool])],
    )
    async with timeout(15), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        async with agent.session(environment=declaration(peer)) as session:
            root = await session.submit("Research.")
            await entered.wait()
            async for event in session.events():
                if isinstance(event, TurnFinished) and event.turn_id != root.turn_id:
                    break
            await root.cancel()
            assert len(received) == 1
            assert (
                await (await session.submit("Continue.")).get_result()
            ).final_output == "following root"


async def test_extractor_preserves_child_tool_failure_flags(responses, tmp_path, monkeypatch):
    captured = []

    @function_tool
    def lookup(key: str) -> ToolOutput:
        return ToolOutput.error("synthetic child tool failure")

    def extract(value):
        captured.append(value)
        return "selected result"

    tool = NativeRoleTool(
        name="research_topic", role="research", description="Research.", extract_result=extract
    )
    responses.respond(collaboration(tool.name, {"input": "Read."}, "failed-child-tool"))
    responses.respond(function_call("lookup", {"key": "bad"}, "failed-produced-child"))
    responses.respond(assistant_output("child recovered"))
    responses.respond(assistant_output("parent result"))
    agent = Agent(
        instructions="Use the research tool.",
        model=ROLE_MODEL,
        provider=provider(responses),
        tools=[NativeAgents(additional_roles=[_role()], managed_tools=[tool]), lookup],
    )
    async with timeout(15), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        async with agent.session(environment=declaration(peer)) as session:
            assert (
                await (await session.submit("Research.")).get_result()
            ).final_output == "parent result"
    assert len(captured) == 1
    items = [
        item for item in captured[0]["items"] if item.get("call_id") == "failed-produced-child"
    ]
    assert [item["status"] for item in items] == ["failed", "failed"]
    assert items[-1]["output"] is None
    assert items[-1]["error"] == "synthetic child tool failure"
