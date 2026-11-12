from __future__ import annotations

import asyncio
import os
import sys
import threading

import pytest

from agents import (
    Agent,
    NativeAgents,
    NativeRoleTool,
    Session,
    ToolOutput,
    function_tool,
    telemetry,
)
from agents.observation import OutputTextDelta, TurnFinished, TurnStarted
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


async def test_managed_result_waits_for_stream_registration(responses, tmp_path, monkeypatch):
    extracted = []
    original = Session._managed_agent_result

    async def delayed_registration(self, state, message):
        turn_id = message["child_turn_id"]
        stream = self._managed_tool_streams.pop(turn_id)

        def register():
            self._managed_tool_streams[turn_id] = stream
            self._observations.notify()

        asyncio.get_running_loop().call_soon(register)
        await original(self, state, message)

    monkeypatch.setattr(Session, "_managed_agent_result", delayed_registration)
    tool = NativeRoleTool(
        name="research_topic",
        role="research",
        description="Research.",
        on_stream=lambda value: None,
        extract_result=lambda value: extracted.append(value) or "selected result",
    )
    responses.respond(collaboration(tool.name, {"input": "Read."}, "ordered-result"))
    responses.respond(assistant_output("child result"))
    responses.respond(assistant_output("parent result"))
    agent = Agent(
        instructions="Research.",
        model=ROLE_MODEL,
        provider=provider(responses),
        tools=[NativeAgents(additional_roles=[_role()], managed_tools=[tool])],
    )
    async with timeout(15), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        async with agent.session(environment=declaration(peer)) as session:
            assert (
                await (await session.submit("Research.")).get_result()
            ).final_output == "parent result"
    assert len(extracted) == 1


async def test_direct_child_cancel_joins_managed_stream(responses, tmp_path, monkeypatch):
    entered, working, cleaning, release, joined = (asyncio.Event() for _ in range(5))
    received = []

    async def observe(value):
        received.append(value)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
            joined.set()

    @function_tool
    async def wait_for_cancel() -> str:
        working.set()
        await asyncio.Event().wait()
        return "unused"

    tool = NativeRoleTool(
        name="research_topic", role="research", description="Research.", on_stream=observe
    )
    responses.respond(collaboration(tool.name, {"input": "Read."}, "cancel-exact-child"))
    responses.respond(function_call("wait_for_cancel", {}, "waiting-child"))
    responses.respond(assistant_output("parent recovered"))
    agent = Agent(
        instructions="Research.",
        model=ROLE_MODEL,
        provider=provider(responses),
        tools=[NativeAgents(additional_roles=[_role()], managed_tools=[tool]), wait_for_cancel],
    )
    async with timeout(15), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        async with agent.session(environment=declaration(peer)) as session:
            root = await session.submit("Research.")
            await entered.wait()
            await working.wait()
            [child] = [item async for item in session.subagents()]
            pending = asyncio.create_task(child.try_cancel(child.turn))
            cleaned = asyncio.create_task(cleaning.wait())
            try:
                await asyncio.wait((pending, cleaned), return_when=asyncio.FIRST_COMPLETED)
                assert cleaning.is_set()
                assert not pending.done()
            finally:
                release.set()
                if pending.done() and not cleaning.is_set():
                    await root.cancel()
                await cleaned
            assert await pending is True
            assert joined.is_set()
            assert len(received) == 1
            assert (await root.get_result()).final_output == "parent recovered"


@pytest.mark.parametrize("kind", ["extract", "stream"])
async def test_managed_callbacks_activate_native_trace_context(
    responses, tmp_path, monkeypatch, kind
):
    if os.environ.get("SDK_MANAGED_TRACE_TEST_CHILD") != "1":
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pytest",
            "-o",
            "pythonpath=",
            "-q",
            __file__ + "::test_managed_callbacks_activate_native_trace_context[" + kind + "]",
            env=dict(os.environ, SDK_MANAGED_TRACE_TEST_CHILD="1"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        assert process.returncode == 0, output.decode()
        return

    class Recorder:
        def on_trace_event(self, event):
            pass

    await telemetry.configure(processors=[Recorder()])
    try:
        seen = []

        def extract(value):
            assert ("event" if kind == "stream" else "items") in value
            seen.append(telemetry.current_context())
            return "selected result" if kind == "extract" else None

        tool = NativeRoleTool(
            name="research_topic",
            role="research",
            description="Research.",
            **({"extract_result": extract} if kind == "extract" else {"on_stream": extract}),
        )
        responses.respond(collaboration(tool.name, {"input": "Read."}, "traced-result"))
        responses.respond(assistant_output("child result"))
        responses.respond(assistant_output("parent result"))
        agent = Agent(
            instructions="Research.",
            model=ROLE_MODEL,
            provider=provider(responses),
            tools=[NativeAgents(additional_roles=[_role()], managed_tools=[tool])],
        )
        async with timeout(15), ExecutorPeer(tmp_path / "remote").serve() as peer:
            trust(peer, tmp_path, monkeypatch)
            async with agent.session(environment=declaration(peer)) as session:
                with telemetry.trace(
                    "managed owner",
                    options={"metadata": {"owner": "managed"}, "include_sensitive_data": False},
                ) as trace:
                    assert (
                        await (await session.submit("Research.")).get_result()
                    ).final_output == "parent result"
        assert seen
        assert all(context is not None for context in seen)
        assert all(context.trace_id == trace.context.trace_id for context in seen)
        assert all(context.trace_metadata == {"owner": "managed"} for context in seen)
        assert all(context.include_sensitive_data is False for context in seen)
    finally:
        await telemetry.shutdown()


@pytest.mark.parametrize("marker_order", ["before_selection", "after_selection", "after_http"])
@pytest.mark.parametrize("prefix_count,pending_limit", [(1, 1), (300, None)])
async def test_selector_first_managed_stream_keeps_full_prefix(
    responses, tmp_path, monkeypatch, prefix_count, pending_limit, marker_order
):
    received, held, selected, delayed = [], {}, {}, []
    prefix_delivered = asyncio.Event()
    marker_received = asyncio.Event()
    http_started = asyncio.get_running_loop().create_future()
    register = Session._register_managed_tool_stream
    select = Session._select_step

    def register_prefix(self, event):
        turn_id = event["msg"]["child_turn_id"]
        state = selected.get(turn_id)
        if state is None:
            held[turn_id] = event
            return
        for index in range(prefix_count):
            self._observations.append(
                OutputTextDelta(
                    session_id=self.id,
                    turn_id=turn_id,
                    submission_id=state.handle.submission_id,
                    item_id="prefix-item",
                    delta=f"prefix-{index}",
                )
            )
        register(self, event)

    def hold_marker(self, event):
        def deliver():
            if marker_order == "before_selection":
                register_prefix(self, event)
            else:
                delayed.append((self, event))
            marker_received.set()

        if marker_order == "after_http":
            http_started.add_done_callback(lambda _: deliver())
        else:
            deliver()

    async def selector_first(self, state, message):
        if message["thread"] != self.id:
            if marker_order == "before_selection":
                await marker_received.wait()
            selected[message["turn_id"]] = state
        marker = held.pop(message["turn_id"], None)
        if marker is not None:
            register_prefix(self, marker)
            await prefix_delivered.wait()
        await select(self, state, message)

    async def before_event(event):
        if len(responses.requests) == 2 and event["type"] == "response.created":
            http_started.set_result(None)
            await marker_received.wait()
            # The child provider request proves its selection has completed.
            # The marker can arrive independently, without a later selector to replay it.
            for session, marker in delayed:
                register_prefix(session, marker)
            delayed.clear()
            await prefix_delivered.wait()

    async def observe(value):
        event = value["event"]
        received.append(event)
        if (
            isinstance(event, OutputTextDelta)
            and event.item_id == "prefix-item"
            and event.delta == f"prefix-{prefix_count - 1}"
        ):
            prefix_delivered.set()

    responses.before_event = before_event
    monkeypatch.setattr(Session, "_register_managed_tool_stream", hold_marker)
    monkeypatch.setattr(Session, "_select_step", selector_first)
    tool = NativeRoleTool(
        name="research_topic",
        role="research",
        description="Research.",
        on_stream=observe,
        on_stream_max_pending_events=pending_limit,
    )
    responses.respond(collaboration(tool.name, {"input": "Read."}, "prefix-managed"))
    responses.respond(assistant_output("child result"))
    responses.respond(assistant_output("parent result"))
    agent = Agent(
        instructions="Use the research tool.",
        model=ROLE_MODEL,
        provider=provider(responses),
        tools=[
            NativeAgents(
                additional_roles=[_role()], managed_tools=[tool], before_step=lambda step: None
            )
        ],
    )
    async with timeout(15), ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        async with agent.session(environment=declaration(peer)) as session:
            assert (
                await (await session.submit("Research.")).get_result()
            ).final_output == "parent result"
    assert not held
    assert isinstance(received[0], TurnStarted)
    assert [
        value.delta
        for value in received
        if isinstance(value, OutputTextDelta) and value.item_id == "prefix-item"
    ] == [f"prefix-{index}" for index in range(prefix_count)]
