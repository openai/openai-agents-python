"""Native children consume only their own accepted function/MCP/context values."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    ChildInvocation,
    HttpMCP,
    MCPResultContext,
    NativeAgents,
    NativeError,
    NativeRole,
    ToolContext,
    function_tool,
)
from agents.observation import (
    ExecutionItemCompleted,
    OutputTextDelta,
    SessionEnded,
    ToolFinished,
    ToolStarted,
    TurnFinished,
    TurnStarted,
    UsageUpdated,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_agent_observation import collect, text_response
from tests.test_native_child_agents import (
    ChildPeer,
    captured_turn,
    child_usage,
    collaboration,
    handles,
    metadata,
    trust,
)
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_tools import Peer, mcp_call


@dataclass
class Memory:
    secret: str = "application-memory-private-49b61"
    mode: str = "normal"
    entries: dict[str, ToolContext[Memory]] = field(default_factory=dict)
    mcp: list[MCPResultContext[Memory]] = field(default_factory=list)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    returned: asyncio.Event = field(default_factory=asyncio.Event)


class FamilyPeer(ChildPeer):
    def __init__(
        self, *, programmatic_tool_calling: bool, execute: bool = False, grandchildren: bool = False
    ):
        super().__init__(("default", "research"))
        self.programmatic_tool_calling = programmatic_tool_calling
        self.execute = execute
        self.grandchildren = grandchildren
        self.steps: dict[str, int] = {}

    async def hold(self, event: dict[str, Any]) -> None:
        # Hold before first dispatch, rather than after the last child output.
        # The parent has therefore completed before either callback starts.
        item = event.get("item", {})
        if item.get("id", "").startswith("begin-"):
            name = item["id"].removeprefix("begin-")
            self.entered[name].set()
            await self.release[name].wait()

    def invoke(self, name: str, step: int) -> dict[str, Any]:
        if step == 0:
            if self.programmatic_tool_calling:
                return code_call(
                    "text('MEMORY:' + JSON.stringify(await tools.lookup("
                    + json.dumps({"key": name})
                    + ")));",
                    "shared-call-id",
                )
            return function_call("lookup", {"key": name}, "shared-call-id")
        if step == 1:
            if self.programmatic_tool_calling:
                return code_call(
                    'text("MCP:" + JSON.stringify('
                    'await tools.mcp__vault__find_record({key:"43"})));',
                    "shared-mcp-id",
                )
            return mcp_call("find_record", "mcp__vault", "shared-mcp-id")
        assert step == 2
        arguments = {"cmd": f"printf {name} > ../{name}.txt", "max_output_tokens": 100}
        if self.programmatic_tool_calling:
            return code_call(
                "text(await tools.exec_command(" + json.dumps(arguments) + "));", "shared-exec-id"
            )
        return function_call("exec_command", arguments, "shared-exec-id")

    def response(self) -> list[dict[str, Any]]:
        body = self.requests[-1]
        meta = metadata(body, self.request_headers[-1])
        path = meta["agent_name"]
        step = self.steps.get(path, 0)
        self.steps[path] = step + 1
        if path == "/root":
            if step == 0:
                return [self.invoke("root", 0)]
            if step == 1:
                calls = []
                for name, role in zip(("alpha", "beta"), self.roles, strict=True):
                    args = {
                        "task_name": name,
                        "message": "Read the requested native work.",
                        "agent_type": role,
                        "fork_turns": "none",
                    }
                    calls.append(collaboration("spawn_agent", args, "spawn-" + name))
                return calls
            return [assistant_output("root complete")]
        name = path.rsplit("/", 1)[-1]
        self.child_requests.setdefault(name, []).append(body)
        self.child_metadata.setdefault(name, []).append(meta)
        if step == 0:
            first = self.invoke(name, 0)
            first["item"]["id"] = "begin-" + name
            return [first]
        if self.grandchildren and name == "alpha" and step == 1:
            args = {"task_name": "grandchild", "message": "Must not start", "fork_turns": "none"}
            return [
                code_call(
                    "text(await tools.collaboration__spawn_agent(" + json.dumps(args) + "));",
                    "forbidden-grandchild",
                )
            ]
        if step == 1 or (self.execute and step == 2):
            return [self.invoke(name, step)]
        return [assistant_output(name + " complete")]


@asynccontextmanager
async def family_peer(
    *, programmatic_tool_calling: bool, execute: bool = False, grandchildren: bool = False
) -> AsyncIterator[FamilyPeer]:
    peer = FamilyPeer(
        programmatic_tool_calling=programmatic_tool_calling,
        execute=execute,
        grandchildren=grandchildren,
    )
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with TestServer(app, shutdown_timeout=1) as server:
        peer.url = str(server.make_url("/v1"))
        try:
            yield peer
        finally:
            for event in peer.release.values():
                event.set()


def family_agent(
    peer: FamilyPeer, executor: ExecutorPeer, mcp_url: str, memory: Memory
) -> Agent[Memory, str]:
    @function_tool
    async def lookup(ctx: ToolContext[Memory], key: str) -> str:
        assert ctx.context is memory
        memory.entries[key] = ctx
        if key == "alpha" and memory.mode != "normal":
            memory.entered.set()
            if memory.mode == "raise":
                raise RuntimeError("callback-exception-private-8053")
            try:
                await memory.release.wait()
            except asyncio.CancelledError:
                # Deliberately slow async cleanup. Its late result must never
                # resolve an already cancelled call, even when a sibling reused
                # the model's same call ID.
                await memory.release.wait()
            memory.returned.set()
        return "accepted:" + key

    async def redact(ctx: MCPResultContext[Memory], value: dict[str, Any]) -> dict[str, Any]:
        assert ctx.context is memory
        assert value["structuredContent"]["receipt"] == "mcp-private-f126"
        memory.mcp.append(ctx)
        assert isinstance(ctx.submission, ChildInvocation)
        text = "accepted-mcp:" + ctx.submission.child_id
        return {"content": [{"type": "text", "text": text}], "isError": False}

    # This is a known model from the unmodified pinned catalog: low reasoning,
    # shell_command, code_mode_only and Responses Lite. Do not replace its
    # metadata with an arbitrary provider test model or a synthetic catalog.
    return Agent(
        instructions="Use native children for the requested work.",
        model="gpt-6.1-sol",
        provider=peer.provider(websocket=False),
        programmatic_tool_calling=peer.programmatic_tool_calling,
        tools=[
            lookup,
            HttpMCP(
                name="vault",
                url=mcp_url,
                tools=["find-record"],
                transform_result=redact,
            ),
            NativeAgents(
                additional_roles=[
                    NativeRole(
                        name="research", description="Read records", instructions="Read once."
                    )
                ],
                # Leave capacity for the rejected grandchild. The regression
                # must exercise child authority, not exhaust the graph quota.
                max_children=3 if peer.grandchildren else 2,
            ),
        ],
    )


@pytest.mark.parametrize(
    ("programmatic_tool_calling", "execute"),
    [
        pytest.param(False, False, id="direct-function-mcp"),
        pytest.param(True, False, id="code-function-mcp"),
        pytest.param(
            True,
            True,
            id="code-function-mcp-native-executor",
            marks=pytest.mark.requires_native_linux_sandbox,
        ),
    ],
)
async def test_two_children_can_use_context_and_mcp_after_root_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, programmatic_tool_calling: bool, execute: bool
) -> None:
    memory = Memory()
    vault = Peer(names=("find-record", "excluded"), label="mcp-private-f126")
    async with (
        timeout(60),
        family_peer(programmatic_tool_calling=programmatic_tool_calling, execute=execute) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)
        if execute:
            for name in ("alpha", "beta"):
                (executor.root.parent / f"{name}.txt").write_text(f"protected-{name}")
        async with family_agent(peer, executor, url, memory).session(
            context=memory, environment=declaration(executor)
        ) as session:
            root = await session.submit("Read a record, then use alpha and beta.")
            await asyncio.gather(*(event.wait() for event in peer.entered.values()))
            result = await (root).get_result()
            assert result.final_output == "root complete"
            assert memory.entries.keys() == {"root"}
            assert memory.entries["root"].submission is root
            await handles(session, 2)
            by_id = {}
            async for child in session.subagents():
                assert child.turn is not None
                await child.turn.steer("Continue this child with its own requested record.")
                by_id[child.id] = child.turn
            alpha = by_id[peer.child_metadata["alpha"][0]["thread_id"]]
            beta = by_id[peer.child_metadata["beta"][0]["thread_id"]]
            for event in peer.release.values():
                event.set()
            first, second = await asyncio.gather((alpha).get_result(), (beta).get_result())
            assert (first.final_output, second.final_output) == ("alpha complete", "beta complete")
            assert memory.entries.keys() == {"root", "alpha", "beta"}
            assert len(memory.mcp) == len(vault.calls) == 2
            for name, handle in (("alpha", alpha), ("beta", beta)):
                ctx = memory.entries[name]
                assert isinstance(ctx.submission, ChildInvocation)
                assert (
                    ctx.submission.session_id,
                    ctx.submission.child_id,
                    ctx.submission.turn_id,
                ) == (session.id, handle.child_id, handle.turn_id)
                assert ctx.submission.instance_id
                assert ctx.submission.submission_id == handle.submission_id
                selected = [item for item in memory.mcp if item.submission == ctx.submission]
                assert len(selected) == 1
                assert selected[0].submission.submission_id == handle.submission_id
                child_wire = json.dumps(peer.child_requests[name], ensure_ascii=False)
                assert "accepted:" + name in child_wire
                assert "accepted-mcp:" + handle.child_id in child_wire
                assert "accepted:" + ({"alpha", "beta"} - {name}).pop() not in child_wire
                events = await collect(handle.events())
                assert any(
                    isinstance(event, ToolStarted) and event.family == "function"
                    for event in events
                )
                assert any(
                    isinstance(event, ToolFinished) and event.family == "function"
                    for event in events
                )
                assert any(
                    isinstance(event, ToolStarted) and event.family == "mcp" for event in events
                )
                assert any(
                    isinstance(event, ToolFinished) and event.family == "mcp" for event in events
                )
                execution = [
                    event.item for event in events if isinstance(event, ExecutionItemCompleted)
                ]
                [mcp] = [item for item in execution if item["type"] == "mcp_call"]
                assert (mcp["thread_id"], mcp["turn_id"]) == (handle.child_id, handle.turn_id)
                assert "accepted-mcp:" + handle.child_id in str(mcp["output"])
                assert "mcp-private-f126" not in str(mcp)
                assert [
                    item
                    async for item in session.items(subagent_id=handle.child_id)
                    if item["type"] in {"mcp_call", "command_execution"}
                ] == execution
                if execute:
                    assert (executor.root.parent / f"{name}.txt").read_text() == f"protected-{name}"
                    response = code_output(peer.child_requests[name][-1], "shared-exec-id")
                    assert response.startswith("Script completed\n"), response
                    _, separator, printed = response.partition("\nOutput:\n")
                    assert separator, response
                    denied = json.loads(printed)
                    assert type(denied["exit_code"]) is int and denied["exit_code"] > 0, denied
                    [command] = [item for item in execution if item["type"] == "command_execution"]
                    assert command["exit_code"] == denied["exit_code"]
                    assert command["native_status"] == "failed"
                    output = denied["output"].lower()
                    assert "permission denied" in output or "read-only file system" in output
            assert memory.entries["alpha"].submission != memory.entries["beta"].submission
            if execute:
                assert any(
                    request.get("method") == "process/start" for request in executor.requests
                )
            if not programmatic_tool_calling:
                assert {ctx.call_id for ctx in memory.entries.values()} == {"shared-call-id"}
            wire = json.dumps(peer.requests, ensure_ascii=False)
            assert memory.secret not in wire
            assert "mcp-private-f126" not in wire
            assert (await (root).get_result()).final_output == result.final_output


async def test_filtered_child_streams_finish_with_their_own_real_text_and_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StreamingFamily(FamilyPeer):
        def response(self) -> list[dict[str, Any]]:
            outputs = super().response()
            if len(outputs) == 1 and outputs[0]["item"].get("role") == "assistant":
                message = outputs[0]["item"]
                content = message["content"][0]["text"]
                return text_response("stream-" + str(len(self.requests)), content[:3], content[3:])
            return outputs

    memory = Memory()
    peer = StreamingFamily(programmatic_tool_calling=False)
    child_usage(
        peer, {"/root": (101, 7, 9, 2), "/root/alpha": (7, 3, 2, 1), "/root/beta": (13, 9, 0, 4)}
    )
    vault = Peer(names=("find-record",), label="mcp-private-f126")
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with (
        timeout(30),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        async with family_agent(peer, executor, url, memory).session(
            context=memory, environment=declaration(executor)
        ) as session:
            all_events = session.events()
            root = await session.submit("Read alpha and beta.")
            await asyncio.gather(*(signal.wait() for signal in peer.entered.values()))
            root_result = await root.get_result()
            await handles(session, 2)
            turns = {}
            async for child in session.subagents():
                assert child.turn is not None
                await child.turn.steer("Finish only your requested record.")
                turns[child.id] = child.turn
            alpha = turns[peer.child_metadata["alpha"][0]["thread_id"]]
            beta = turns[peer.child_metadata["beta"][0]["thread_id"]]
            streams = [asyncio.create_task(collect(turn.events())) for turn in (alpha, beta)]
            for release in peer.release.values():
                release.set()
            results = await asyncio.gather(alpha.get_result(), beta.get_result())
            assert [result.final_output for result in results] == [
                "alpha complete",
                "beta complete",
            ]
            assert all(result.usage is not None for result in results)
            # A shared Turn stream must settle while its Session stays open.
            # Before the repair both real native waits finish, but these
            # consumer-side filters otherwise wait until Session close.
            observed = await asyncio.wait_for(asyncio.gather(*streams), 2)
            for turn, result, events in zip((alpha, beta), results, observed, strict=True):
                assert all(event.submission_id == turn.submission_id for event in events)
                assert sum(isinstance(event, TurnStarted) for event in events) == 1
                assert isinstance(events[-1], TurnFinished)
                assert (events[-1].status, events[-1].error_type) == ("completed", None)
                assert (
                    "".join(event.delta for event in events if isinstance(event, OutputTextDelta))
                    == result.final_output
                )
                usages = [event.usage for event in events if isinstance(event, UsageUpdated)]
                assert usages and usages[-1] == result.usage
            assert results[0].usage != results[1].usage
            assert await root_result.turn.get_result() is root_result
        retained = [event async for event in all_events]
        for turn in (root, alpha, beta):
            selected = [
                event
                for event in retained
                if getattr(event, "submission_id", None) == turn.submission_id
            ]
            assert sum(isinstance(event, TurnStarted) for event in selected) == 1
            assert sum(isinstance(event, TurnFinished) for event in selected) == 1


@pytest.mark.parametrize("terminal_only", [False, True])
async def test_child_native_mcp_timeout_before_python_handoff_finishes_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal_only: bool
) -> None:
    from agents import _native

    memory = Memory()
    vault = Peer(names=("find-record",), label="mcp-private-f126")
    before_handoff, release_handoff = asyncio.Event(), asyncio.Event()
    original_next = _native.NativeThread._next_native_mcp_result
    original_event = _native.NativeThread._next_session_event
    async with (
        timeout(30),
        family_peer(programmatic_tool_calling=False) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)

        async def delay_original_notice(thread):
            raw = await original_next(thread)
            notice = json.loads(raw)
            if (
                notice is not None
                and "alpha" in peer.child_metadata
                and notice["thread_id"] == peer.child_metadata["alpha"][0]["thread_id"]
            ):
                # Hold only the real queued notice before Python hands it off.
                # The native deadline, grant retirement and receipt are real.
                before_handoff.set()
                await release_handoff.wait()
            return raw

        monkeypatch.setattr(_native.NativeThread, "_next_native_mcp_result", delay_original_notice)
        if terminal_only:

            async def skip_early_policy_notice(thread):
                while True:
                    raw = await original_event(thread)
                    event = json.loads(raw)
                    if not (
                        event["msg"]["type"] == "_sdk_model_generation_failed"
                        and event.get("subagent_id") == peer.child_metadata["alpha"][0]["thread_id"]
                        and event["msg"].get("policy_stop") is True
                    ):
                        return raw

            # Exercise settlement with the real terminal event alone. The
            # earlier notice may be delayed or retired by native completion.
            monkeypatch.setattr(
                _native.NativeThread, "_next_session_event", skip_early_policy_notice
            )
        agent = family_agent(peer, executor, url, memory)
        # Keep the family's installed result transformer when changing its deadline.
        for tool in agent.tools:
            if isinstance(tool, HttpMCP):
                assert tool.transform_result is not None
        agent = replace(
            agent,
            tools=tuple(
                replace(tool, transform_result_timeout=0.25)
                if isinstance(tool, HttpMCP) and tool.transform_result is not None
                else tool
                for tool in agent.tools
            ),
        )
        async with agent.session(context=memory, environment=declaration(executor)) as session:
            try:
                root = await session.submit("Read the two records")
                await asyncio.gather(*(signal.wait() for signal in peer.entered.values()))
                root_result = await root.get_result()
                turns = {turn.child_id: turn for turn in await handles(session, 2)}
                alpha = turns[peer.child_metadata["alpha"][0]["thread_id"]]
                beta = turns[peer.child_metadata["beta"][0]["thread_id"]]
                alpha_stream = asyncio.create_task(collect(alpha.events()))
                peer.release["alpha"].set()
                await before_handoff.wait()
                with pytest.raises(
                    NativeError, match="^Native MCP result policy failed$"
                ) as failure:
                    await asyncio.wait_for(alpha.get_result(), 4)
                assert failure.value.__cause__ is failure.value.__context__ is None
                assert memory.mcp == []
                owner = next(
                    state
                    for state in session._child_callbacks.values()
                    if state.handle.submission_id == alpha.submission_id
                )
                # Expiry stops this child before another model request, even
                # while its unapproved result notice remains outside Python.
                assert owner.failure.done()
                assert str(owner.failure.result()) == "Native MCP result policy failed"
                assert len(peer.child_requests["alpha"]) == 2
                if terminal_only:
                    # Early failure can settle callbacks before Python consumes
                    # the terminal. Only this path needs terminal delivery.
                    assert owner.terminal.done()
                    assert owner.terminal.result().get("last_agent_message") != "alpha complete"
                observed = await asyncio.wait_for(alpha_stream, 2)
                assert sum(isinstance(event, TurnStarted) for event in observed) == 1
                assert isinstance(observed[-1], TurnFinished)
                assert (observed[-1].status, observed[-1].error_type) == ("failed", "NativeError")
                release_handoff.set()
                peer.release["beta"].set()
                beta_result = await beta.get_result()
                assert beta_result.final_output == "beta complete"
                beta_events = await asyncio.wait_for(collect(beta.events()), 2)
                assert isinstance(beta_events[-1], TurnFinished)
                assert beta_events[-1].status == "completed"
                assert len(memory.mcp) == 1
                assert memory.mcp[0].submission.submission_id == beta.submission_id
                assert await root_result.turn.get_result() is root_result
                assert "mcp-private-f126" not in json.dumps(peer.child_requests)
            finally:
                release_handoff.set()
                for release in peer.release.values():
                    release.set()


async def test_child_reader_loss_reports_owned_finishes_before_failed_session_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = Memory()
    vault = Peer(names=("find-record",), label="mcp-private-f126")
    async with (
        timeout(30),
        family_peer(programmatic_tool_calling=False) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)
        async with family_agent(peer, executor, url, memory).session(
            context=memory, environment=declaration(executor)
        ) as session:
            try:
                observed = asyncio.create_task(collect(session.events()))
                root = await session.submit("Start two children")
                await asyncio.gather(*(signal.wait() for signal in peer.entered.values()))
                root_result = await root.get_result()
                turns = await handles(session, 2)
                # Simulate loss of the actual SDK event reader as in the
                # existing native reader-loss test; no native event is forged.
                session._reader.cancel()
                await session._reader
                events = await asyncio.wait_for(observed, 4)
                assert isinstance(events[-1], SessionEnded)
                assert events[-1].reason == "failed"
                for turn in turns:
                    selected = [
                        event
                        for event in events
                        if getattr(event, "submission_id", None) == turn.submission_id
                    ]
                    assert sum(isinstance(event, TurnStarted) for event in selected) == 1
                    assert isinstance(selected[-1], TurnFinished)
                    assert selected[-1].status == "failed"
                    assert selected[-1].sequence < events[-1].sequence
                    with pytest.raises(NativeError):
                        await asyncio.wait_for(turn.get_result(), 2)
                assert await root_result.turn.get_result() is root_result
            finally:
                for release in peer.release.values():
                    release.set()


@pytest.mark.parametrize("cancel_waiter", [False, True])
@pytest.mark.parametrize("sync_callback", [False, True])
async def test_cancel_waits_for_late_child_callback_and_leaves_sibling_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_waiter: bool, sync_callback: bool
) -> None:
    memory = Memory(mode="hold")
    worker_release = threading.Event()
    loop = asyncio.get_running_loop()

    @function_tool(name="lookup")
    def sync_lookup(ctx: ToolContext[Memory], key: str) -> str:
        memory.entries[key] = ctx
        if key == "alpha":
            loop.call_soon_threadsafe(memory.entered.set)
            worker_release.wait()
            loop.call_soon_threadsafe(memory.returned.set)
        return "accepted:" + key

    vault = Peer(names=("find-record",), label="mcp-private-f126")
    async with (
        timeout(60),
        family_peer(programmatic_tool_calling=False) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)
        agent = family_agent(peer, executor, url, memory)
        if sync_callback:
            agent = replace(agent, tools=[sync_lookup, *agent.tools[1:]])
        async with agent.session(context=memory, environment=declaration(executor)) as session:
            pending = None
            try:
                root = await session.submit("Start alpha and beta")
                await asyncio.gather(*(event.wait() for event in peer.entered.values()))
                assert (await (root).get_result()).final_output == "root complete"
                by_id = {turn.child_id: turn for turn in await handles(session, 2)}
                alpha = by_id[peer.child_metadata["alpha"][0]["thread_id"]]
                beta = by_id[peer.child_metadata["beta"][0]["thread_id"]]
                peer.release["alpha"].set()
                await memory.entered.wait()
                child = [item async for item in session.subagents() if item.id == alpha.child_id][0]
                pending = asyncio.create_task(child.try_cancel(alpha))
                [owner] = session._callbacks_for_child(alpha)
                await asyncio.shield(owner.terminal)
                assert await child.try_cancel(alpha) is False
                if cancel_waiter:
                    pending.cancel()
                peer.release["beta"].set()
                assert (await (beta).get_result()).final_output == "beta complete"
                assert not pending.done(), "The cancelled Turn must still join its running callback"
                assert not memory.returned.is_set()
                later = await child.start("Continue this child while old cleanup drains")
                assert later.turn_id != alpha.turn_id
                assert (await later.get_result()).final_output == "alpha complete"
                assert not pending.done(), "The successor must not release the original callback"
                memory.release.set()
                worker_release.set()
                if cancel_waiter:
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                else:
                    assert await pending is True
                with pytest.raises(NativeError):
                    await alpha.get_result()
                assert await child.try_cancel(alpha) is False
                assert memory.returned.is_set()
                assert memory.entries["alpha"].call_id == memory.entries["beta"].call_id
                wire = json.dumps(peer.requests, ensure_ascii=False)
                assert memory.secret not in wire and "accepted:alpha" not in wire
                assert "accepted:beta" in wire
                assert len(vault.calls) == 2
            finally:
                # Test barriers must release before Session joins callback cleanup.
                memory.release.set()
                worker_release.set()
                for release in peer.release.values():
                    release.set()
                if pending is not None:
                    await asyncio.gather(pending, return_exceptions=True)


async def test_failed_child_callback_releases_its_native_pending_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = Memory(mode="raise")
    vault = Peer(names=("find-record",), label="mcp-private-f126")
    async with (
        timeout(60),
        family_peer(programmatic_tool_calling=False) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)
        async with family_agent(peer, executor, url, memory).session(
            context=memory, environment=declaration(executor)
        ) as session:
            root = await session.submit("Start alpha and beta")
            await asyncio.gather(*(event.wait() for event in peer.entered.values()))
            assert (await (root).get_result()).final_output == "root complete"
            by_id = {turn.child_id: turn for turn in await handles(session, 2)}
            alpha = by_id[peer.child_metadata["alpha"][0]["thread_id"]]
            beta = by_id[peer.child_metadata["beta"][0]["thread_id"]]
            for event in peer.release.values():
                event.set()
            with pytest.raises(RuntimeError, match="callback-exception-private-8053"):
                await (alpha).get_result()
            assert (await (beta).get_result()).final_output == "beta complete"
            assert (await (root).get_result()).final_output == "root complete"
            assert len(vault.calls) == 1
            wire = json.dumps(peer.requests, ensure_ascii=False)
            assert "callback-exception-private-8053" not in wire
            assert "accepted:alpha" not in wire and memory.secret not in wire


class PendingChildCallsPeer(ChildPeer):
    """Keep beta at its final response while alpha has overlapping tool owners."""

    def __init__(self) -> None:
        super().__init__(("default", "research"))

    def response(self) -> list[dict[str, Any]]:
        body = self.requests[-1]
        meta = metadata(body, self.request_headers[-1])
        if meta["agent_name"] == "/root":
            return super().response()
        name = meta["agent_name"].rsplit("/", 1)[-1]
        self.child_metadata.setdefault(name, []).append(meta)
        requests = self.child_requests.setdefault(name, [])
        requests.append(body)
        if name == "alpha" and len(requests) == 1:
            # "ready" returns only when A and C have reached application code.
            # Hence B's native request must be delivered after both, regardless
            # of how the host schedules concurrent Javascript promises.
            return [
                code_call(
                    "const a = tools.mcp__vault__a({key:'record'});"
                    "const c = tools.mcp__vault__c({key:'record'});"
                    "await tools.mcp__vault__ready({key:'record'});"
                    "const b = tools.mcp__vault__b({key:'record'});"
                    "text(await Promise.all([a, c, b]));",
                    "concurrent-child-calls",
                )
            ]
        result = assistant_output(name + " complete")
        if name == "beta":
            result["item"]["id"] = "done-beta"
        return [result]


class PendingReadOnlyPeer(Peer):
    def __init__(self, *, late_remote_c: bool) -> None:
        super().__init__(names=("a", "b", "c", "ready"))
        self.c_entered = asyncio.Event()
        self.release_c = asyncio.Event()
        if not late_remote_c:
            self.release_c.set()

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("method") == "tools/call" and body["params"]["name"] == "c":
            self.c_entered.set()
            await self.release_c.wait()
        response = await super().handle(request)
        if body.get("method") == "initialize":
            # Cached catalogs strip readOnlyHint. This test's parallel callback
            # barrier requires a live catalog for each child.
            payload = json.loads(response.text or "{}")
            payload["result"]["capabilities"]["experimental"] = {
                "codex/tool-catalog-cache": {"cacheable": False}
            }
            return web.json_response(payload)
        return response


@pytest.mark.parametrize("late_remote_c", [False, True], ids=["held-c-policy", "late-c-response"])
async def test_pending_child_input_survives_failed_parallel_mcp_policies_and_all_owners_retire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, late_remote_c: bool
) -> None:
    # Native MCP's readOnlyHint admits real parallel calls without changing the
    # selected policy. The executor provides children's transport, no shell runs.
    peer = PendingChildCallsPeer()
    vault = PendingReadOnlyPeer(late_remote_c=late_remote_c)
    # Keep cache eligibility independent of the host's OAuth credential store.
    monkeypatch.setenv("TEST_PENDING_CHILD_MCP_TOKEN", "synthetic-loopback-only")
    entered = {key: asyncio.Event() for key in ("a", "c")}
    allow_a_failure, c_joined = asyncio.Event(), asyncio.Event()
    release_c = asyncio.Event()
    entries: dict[str, MCPResultContext[None]] = {}
    original_error = RuntimeError("private-concurrent-child-A-57273")

    async def accept(ctx: MCPResultContext[None], result: dict[str, Any]) -> dict[str, Any]:
        assert isinstance(ctx.submission, ChildInvocation)
        key = ctx.tool_name
        entries[key] = ctx
        if key == "a":
            entered[key].set()
            await allow_a_failure.wait()
            raise original_error
        if key == "c":
            entered[key].set()
            try:
                await release_c.wait()
            finally:
                c_joined.set()
        if key == "ready":
            await asyncio.gather(
                entered["a"].wait(),
                (vault.c_entered if late_remote_c else entered["c"]).wait(),
            )
        return {"content": [{"type": "text", "text": "accepted:" + key}], "isError": False}

    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with (
        timeout(40),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as vault_url,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = Agent(
            instructions="Use native children for the requested work.",
            model="gpt-6.1-sol",
            programmatic_tool_calling=True,
            provider=peer.provider(websocket=False),
            tools=[
                HttpMCP(
                    name="vault",
                    url=vault_url,
                    tools=list(vault.names),
                    bearer_token=os.environ["TEST_PENDING_CHILD_MCP_TOKEN"],
                    transform_result=accept,
                    transform_result_timeout=25,
                ),
                NativeAgents(
                    additional_roles=[
                        NativeRole(
                            name="research", description="Read records", instructions="Read once."
                        )
                    ],
                    max_children=2,
                ),
            ],
        )
        try:
            async with agent.session(environment=declaration(executor)) as session:
                root = await session.submit("Start alpha and beta")
                assert (await (root).get_result()).final_output == "root complete"
                try:
                    async with timeout(8):
                        await peer.entered["beta"].wait()
                        stream = session.events()
                        try:
                            async for event in stream:
                                if isinstance(event, ToolFinished) and event.name == "b":
                                    assert event.status == "completed"
                                    break
                            else:
                                pytest.fail("MCP tool B never finished")
                        finally:
                            await stream.aclose()
                except TimeoutError:
                    model_calls = [(n, len(r)) for n, r in peer.child_requests.items()]
                    mcp_requests = [
                        (r.get("method"), r.get("params", {}).get("name")) for r in vault.requests
                    ]
                    pytest.fail(
                        f"Child setup never reached A+C+B: entered={set(entries)}; "
                        f"beta reached={peer.entered['beta'].is_set()}; "
                        f"child model calls={model_calls}; "
                        f"C reached server={vault.c_entered.is_set()}; "
                        f"MCP requests={mcp_requests}"
                    )
                expected = {"a", "b", "ready"} if late_remote_c else {"a", "b", "c", "ready"}
                assert set(entries) == expected
                assert not c_joined.is_set()
                by_id = {turn.child_id: turn for turn in await handles(session, 2)}
                alpha = by_id[peer.child_metadata["alpha"][0]["thread_id"]]
                beta = by_id[peer.child_metadata["beta"][0]["thread_id"]]
                assert all(ctx.submission.turn_id == alpha.turn_id for ctx in entries.values())
                peer.action = collaboration(
                    "send_message",
                    {"target": "/root/alpha", "message": "PENDING_A57273: retain this input."},
                    "queued-message",
                )
                sent = await session.submit("Queue further input to alpha")
                assert (await (sent).get_result()).final_output == "root complete"
                # Core's documented conditional cancel must preserve pending
                # input. The callback-failure path must retire all old grants.
                assert await session._native._cancel_child_turn((alpha)._native) is False
                allow_a_failure.set()
                peer.release["beta"].set()
                assert (await (beta).get_result()).final_output == "beta complete"
                if late_remote_c:
                    # Only place the race with private state: C's server result
                    # arrives after this exact turn's Python owner has settled.
                    # Progress and policy non-entry are asserted via public
                    # callbacks and Session.wait below.
                    states = session._callbacks_for_child(alpha)
                    assert len(states) == 1
                    async with timeout(8):
                        await asyncio.shield(states[0].completion)
                    assert states[0].stopping
                    assert "c" not in entries
                    vault.release_c.set()
                async with timeout(8):
                    # The caller sees the public MCP policy error, not peer data
                    # or the private exception raised by the application.
                    with pytest.raises(NativeError, match="^Native MCP result policy failed$"):
                        await (alpha).get_result()
                if late_remote_c:
                    assert "c" not in entries, "Stopped child must not admit a late MCP policy"
                else:
                    assert c_joined.is_set(), (
                        "Wait must join held C after retiring its native owner"
                    )
                assert not release_c.is_set(), "C must stop without a successful late result"

                # QueueOnly does not have to start a turn on its own. A later
                # explicit task must reach the same child and retain its input.
                peer.action = collaboration(
                    "followup_task",
                    {"target": "/root/alpha", "message": "Continue and apply the queued input."},
                    "wake-queued-message",
                )
                followed = await session.submit("Continue alpha")
                assert (await (followed).get_result()).final_output == "root complete"
                async with timeout(8):
                    current = await captured_turn(session, alpha.child_id)
                    while current is None or current.turn_id == alpha.turn_id:
                        await asyncio.sleep(0)
                        current = await captured_turn(session, alpha.child_id)
                    assert (await (current).get_result()).final_output == "alpha complete"
                assert current.instance_id == alpha.instance_id
                wire = json.dumps(peer.child_requests["alpha"], ensure_ascii=False)
                assert "PENDING_A57273" in wire
                assert str(original_error) not in wire
        finally:
            # These releases are teardown only; none is needed for a passing
            # test or for the terminal callback/wait and queued followup above.
            allow_a_failure.set()
            release_c.set()
            vault.release_c.set()
            peer.release["beta"].set()


async def test_child_cannot_spawn_grandchildren_from_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = Memory()
    vault = Peer(names=("find-record",), label="mcp-private-f126")
    async with (
        timeout(60),
        family_peer(programmatic_tool_calling=True, grandchildren=True) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        vault.serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)
        async with family_agent(peer, executor, url, memory).session(
            context=memory, environment=declaration(executor)
        ) as session:
            root = await session.submit("Start alpha and beta")
            await asyncio.gather(*(event.wait() for event in peer.entered.values()))
            assert (await (root).get_result()).final_output == "root complete"
            by_id = {turn.child_id: turn for turn in await handles(session, 2)}
            alpha = by_id[peer.child_metadata["alpha"][0]["thread_id"]]
            beta = by_id[peer.child_metadata["beta"][0]["thread_id"]]
            for event in peer.release.values():
                event.set()
            # Native code can report an unavailable function as its cell result
            # or end the whole turn. Either way it must not create a child.
            try:
                await (alpha).get_result()
            except NativeError:
                pass
            assert (await (beta).get_result()).final_output == "beta complete"
            children = [item async for item in session.subagents()]
            assert {item.id for item in children} == {alpha.child_id, beta.child_id}
            assert set(peer.child_requests) == {"alpha", "beta"}
