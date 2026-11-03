"""Environment StopTurn must retire the actual owning child turn."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    Allow,
    ChildInvocation,
    ExecutorContext,
    HttpMCP,
    NativeAgents,
    NativeError,
    NativeRole,
    Session,
    StopTurn,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, tool_output
from tests.test_native_child_agents import (
    ChildPeer,
    handles,
    metadata,
    trust,
)
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_tools import Peer
from tests.test_native_stop_boundaries import CleanupBarrier


class YieldedEnvironmentPeer(ChildPeer):
    """Return the first real cell's ID to a later explicit child turn."""

    def __init__(self) -> None:
        super().__init__(("default", "research"))
        self.cell_id: dict[str, str] = {}
        self.after_wait: dict[str, str] = {}
        self.successor: set[str] = set()

    def response(self) -> list[dict[str, Any]]:
        body = self.requests[-1]
        meta = metadata(body, self.request_headers[-1])
        if meta["agent_name"] == "/root":
            return super().response()
        name = meta["agent_name"].rsplit("/", 1)[-1]
        self.child_metadata.setdefault(name, []).append(meta)
        requests = self.child_requests.setdefault(name, [])
        requests.append(body)
        if name in self.successor:
            if name not in self.after_wait:
                wait_results = [
                    item
                    for item in body.get("input", [])
                    if item.get("call_id") == "read-original-" + name
                    and item.get("type") == "function_call_output"
                ]
                if not wait_results:
                    return [
                        function_call(
                            "wait",
                            {"cell_id": self.cell_id[name], "yield_time_ms": 1},
                            "read-original-" + name,
                        )
                    ]
                self.after_wait[name] = tool_output(body, "read-original-" + name)
            return [assistant_output(name + " successor")]
        if len(requests) == 1:
            return [code_call("yield_control(); await new Promise(() => {});", "shared-running-id")]
        if len(requests) == 2:
            output = code_output(body, "shared-running-id")
            prefix = "Script running with cell ID "
            assert output.startswith(prefix), output
            self.cell_id[name] = output[len(prefix) :].split()[0].rstrip(".")
            if name == "alpha":
                return [
                    code_call(
                        'text(await tools.exec_command({cmd:"printf stopped > forbidden"}));',
                        "shared-call-id",
                    )
                ]
        return [assistant_output(name + " complete")]


async def test_environment_stop_joins_all_owned_yielded_cells_not_sibling_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Terminal or a local failed callback alone must not leave an owned cell running."""
    peer = YieldedEnvironmentPeer()
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    contexts = []

    def approval(ctx: ExecutorContext[None], arguments: Any) -> bool | StopTurn:
        contexts.append(ctx)
        return StopTurn("private") if ctx.agent_role == "default" else True

    async with (
        timeout(40),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = Agent(
            instructions="Use native children and tools.",
            model="gpt-5.5",
            provider=peer.provider(websocket=False),
            programmatic_tool_calling=True,
            tools=[
                NativeAgents(
                    additional_roles=[
                        NativeRole(
                            name="research", description="Read a record", instructions="Read."
                        )
                    ]
                )
            ],
        )
        async with agent.session(environment=declaration(executor, approval=approval)) as session:
            root = await session.submit("Start two independent native children.")
            assert (await root.get_result()).final_output == "root complete"
            first = await handles(session, 2)
            outcomes = await asyncio.gather(
                *(t.get_result() for t in first), return_exceptions=True
            )
            [ctx] = contexts
            assert isinstance(ctx.submission, ChildInvocation)
            assert len(peer.child_requests["alpha"]) == 2
            assert not (executor.root / "forbidden").exists()
            for turn, result in zip(first, outcomes, strict=True):
                if turn.child_id == ctx.thread_id:
                    assert isinstance(result, NativeError)
                else:
                    assert not isinstance(result, BaseException)
                    assert result.final_output == "beta complete"
            # A live, independently queued explicit turn can observe its original cell.
            # Matching child and call IDs cannot confer authority across incarnations.
            peer.successor.update(("alpha", "beta"))
            fresh = [
                await child.start("Wait on your original running cell.")
                async for child in session.subagents()
            ]
            later = await asyncio.gather(*(t.get_result() for t in fresh), return_exceptions=True)
            assert all(not isinstance(result, BaseException) for result in later), later
            assert "Script running with cell ID " in peer.after_wait["beta"], peer.after_wait
            assert "Script running with cell ID " not in peer.after_wait["alpha"], peer.after_wait
            assert "Script running" not in peer.after_wait["alpha"], peer.after_wait
            assert (await root.get_result()).final_output == "root complete"


async def test_child_stop_waits_for_delayed_native_receipt_before_joining_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live Python request can arrive before its child lifecycle receipt."""
    held_tool = asyncio.Event()
    released_tool = asyncio.Event()
    notices = []

    @function_tool
    async def keep_pending() -> str:
        held_tool.set()
        try:
            await asyncio.Event().wait()
        finally:
            released_tool.set()
        return "not reached"

    class EarlyCallbackPeer(YieldedEnvironmentPeer):
        def response(self) -> list[dict[str, Any]]:
            response = super().response()
            body = self.requests[-1]
            name = metadata(body, self.request_headers[-1])["agent_name"]
            if (
                name == "/root/alpha"
                and "alpha" not in self.successor
                and len(self.child_requests["alpha"]) == 2
            ):
                return [
                    code_call(
                        "const pending = tools.keep_pending({});"
                        'text(await tools.exec_command({cmd:"printf stopped > forbidden"}));'
                        "await pending;",
                        "shared-call-id",
                    )
                ]
            return response

    peer = EarlyCallbackPeer()
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    original_read = Session._read_child_event
    delayed: list[tuple[Session, dict[str, Any]]] = []
    deliver = False

    async def delay_receipts(session: Session, event: dict[str, Any]) -> None:
        # Defer only the real lifecycle receipts, leaving the independent
        # Python and Environment callback channels free to deliver requests.
        if not deliver:
            delayed.append((session, event))
        else:
            await original_read(session, event)

    monkeypatch.setattr(Session, "_read_child_event", delay_receipts)

    class OriginalResponsePort:
        def __init__(self, native):
            self.native = native
            self.interrupted = asyncio.Event()

        def __getattr__(self, name):
            return getattr(self.native, name)

        async def _interrupt_child_callback(self, request):
            stopped = await self.native._interrupt_child_callback(request)
            if stopped:
                self.interrupted.set()
            return stopped

    async def approval(ctx: ExecutorContext[None], arguments: Any) -> StopTurn:
        notices.append(ctx)
        await held_tool.wait()
        return StopTurn("private")

    async with (
        timeout(40),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = Agent(
            instructions="Use native children and tools.",
            model="gpt-5.5",
            provider=peer.provider(websocket=False),
            tools=[
                keep_pending,
                NativeAgents(
                    additional_roles=[
                        NativeRole(
                            name="research", description="Read a record", instructions="Read."
                        )
                    ]
                ),
            ],
            programmatic_tool_calling=True,
        )
        async with agent.session(environment=declaration(executor, approval=approval)) as session:
            port = OriginalResponsePort(session._native)
            session._native = port
            try:
                root = await session.submit("Use both children.")
                assert (await root.get_result()).final_output == "root complete"
                await port.interrupted.wait()
                [ctx] = notices
                assert isinstance(ctx.submission, ChildInvocation)
                assert delayed, "The native lifecycle receipt must actually have arrived"
                # Release the real queued receipt only after native acknowledged
                # the original pending callback's exact-owner interrupt.
                deliver = True
                for owner, event in delayed:
                    await original_read(owner, event)
                delayed.clear()

                first = await handles(session, 2)
                alpha = next(t for t in first if t.child_id == ctx.thread_id)
                beta = next(t for t in first if t is not alpha)
                with pytest.raises(NativeError, match="^Turn stopped by policy$") as stopped:
                    await alpha.get_result()
                assert stopped.value.__context__ is None
                assert released_tool.is_set()
                assert (await beta.get_result()).final_output == "beta complete"
                assert not (executor.root / "forbidden").exists()
                assert len(peer.child_requests["alpha"]) == 2
                peer.successor.update(("alpha", "beta"))
                fresh = [
                    await child.start("Wait on your original running cell.")
                    async for child in session.subagents()
                ]
                later = await asyncio.gather(
                    *(t.get_result() for t in fresh), return_exceptions=True
                )
                assert all(not isinstance(result, BaseException) for result in later), later
                assert "Script running with cell ID " in peer.after_wait["beta"], peer.after_wait
                assert "Script running" not in peer.after_wait["alpha"], peer.after_wait
                assert (await root.get_result()).final_output == "root complete"
            finally:
                deliver = True
                for owner, event in delayed:
                    await original_read(owner, event)


class EnvironmentChildPeer(ChildPeer):
    def __init__(self, kind: str) -> None:
        super().__init__(("default", "research"))
        self.kind = kind

    def response(self) -> list[dict[str, Any]]:
        body = self.requests[-1]
        meta = metadata(body, self.request_headers[-1])
        if meta["agent_name"] == "/root":
            return super().response()
        name = meta["agent_name"].rsplit("/", 1)[-1]
        self.child_metadata.setdefault(name, []).append(meta)
        requests = self.child_requests.setdefault(name, [])
        requests.append(body)
        if len(requests) == 1:
            if name == "alpha":
                args = (
                    {"cmd": "printf forbidden >> selected-effect"}
                    if self.kind == "exec_command"
                    else "*** Begin Patch\n*** Add File: selected-effect\n+forbidden\n*** End Patch"
                )
                body = (
                    "try { await tools." + self.kind + "(" + json.dumps(args) + ");"
                    "} catch (denial) {} "
                    "text(await tools.after_selected({}));"
                )
            else:
                body = "text(await tools.survived({}));"
            return [code_call(body, "shared-call-id")]
        return [assistant_output(name + " complete")]


@pytest.mark.parametrize("kind", ["exec_command", "apply_patch"])
@pytest.mark.parametrize("decision", ["before", "approval", "approval-false"])
async def test_child_code_stop_prevents_caught_denial_following_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, decision: str
) -> None:
    """Real generated code must not turn a whole-turn stop into a local error."""
    after: list[str] = []
    survivors: list[str] = []
    contexts: list[ExecutorContext[None]] = []

    @function_tool
    def after_selected() -> str:
        after.append("effect")
        return "AFTER_SELECTED_EFFECT"

    @function_tool
    def survived() -> str:
        survivors.append("effect")
        return "survived"

    def before(ctx: ExecutorContext[None], args: Any) -> Allow | StopTurn:
        contexts.append(ctx)
        return StopTurn("private-source-reason") if ctx.agent_role == "default" else Allow()

    def approval(ctx: ExecutorContext[None], args: Any) -> bool | StopTurn:
        contexts.append(ctx)
        if ctx.agent_role != "default":
            return True
        return False if decision == "approval-false" else StopTurn("private-source-reason")

    peer = EnvironmentChildPeer(kind)
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    policy = {"transform_arguments": before} if decision == "before" else {"approval": approval}
    async with (
        timeout(40),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = Agent(
            instructions="Use native children and tools.",
            provider=peer.provider(websocket=False),
            model="gpt-5.5",
            tools=[
                after_selected,
                survived,
                NativeAgents(
                    additional_roles=[
                        NativeRole(
                            name="research", description="Read a record", instructions="Read."
                        )
                    ]
                ),
            ],
            programmatic_tool_calling=True,
        )
        async with agent.session(environment=declaration(executor, **policy)) as session:
            root = await session.submit("Run both children.")
            assert (await root.get_result()).final_output == "root complete"

            child_turns = await handles(session, 2)
            results = await asyncio.gather(
                *(t.get_result() for t in child_turns), return_exceptions=True
            )
            [ctx] = contexts
            assert isinstance(ctx.submission, ChildInvocation)
            assert ctx.session is session and ctx.agent is agent
            assert ctx.outer_call_id == "shared-call-id"
            assert ctx.cell_id and ctx.runtime_tool_call_id
            assert ctx.agent_role == "default"
            # Independent effects and native continuation, checked before the status.
            assert survivors == ["effect"]
            assert not [
                r
                for r in executor.requests
                if r.get("method") in {"process/start", "fs/writeFile"}
                and "selected-effect" in str(r)
            ], executor.requests
            assert not (executor.root / "selected-effect").exists()
            assert after == []
            assert len(peer.child_requests["alpha"]) == 1
            assert "AFTER_SELECTED_EFFECT" not in json.dumps(peer.requests)
            for child, result in zip(child_turns, results, strict=True):
                if child.child_id == ctx.thread_id:
                    assert child.turn_id == ctx.turn_id
                    assert isinstance(result, NativeError)
                    assert str(result) == "Turn stopped by policy"
                else:
                    assert not isinstance(result, BaseException)
                    assert result.final_output == "beta complete"
            assert (await root.get_result()).final_output == "root complete"


class LateStopPeer(ChildPeer):
    """The real model loop ends after a still-live intercepted cell yields."""

    def __init__(self, entered: asyncio.Event) -> None:
        super().__init__(("default", "research"))
        self.entered_policy = entered

    async def hold(self, event: dict[str, Any]) -> None:
        if event.get("item", {}).get("id") == "terminal-alpha":
            await self.entered_policy.wait()

    def response(self) -> list[dict[str, Any]]:
        body = self.requests[-1]
        meta = metadata(body, self.request_headers[-1])
        if meta["agent_name"] == "/root":
            return super().response()
        name = meta["agent_name"].rsplit("/", 1)[-1]
        self.child_metadata.setdefault(name, []).append(meta)
        requests = self.child_requests.setdefault(name, [])
        requests.append(body)
        if len(requests) == 1:
            if name == "alpha":
                return [
                    code_call(
                        "const selected = tools.exec_command("
                        '{cmd:"printf selected > must-not-exist"});'
                        "yield_control(); try { await selected; } catch(denied) {}"
                        'await tools.mcp__following__search({key:"late-unhooked-alpha-effect"});'
                        'text("LATE-ALPHA-RESULT");',
                        "shared-call-id",
                    )
                ]
            return [
                code_call(
                    'text(await tools.mcp__following__search({key:"surviving-beta-effect"}));',
                    "shared-call-id",
                )
            ]
        result = assistant_output(name + " complete")
        result["item"]["id"] = "terminal-" + name
        return [result]


async def test_environment_child_stop_after_real_native_terminal_prevents_unhooked_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, decide = asyncio.Event(), asyncio.Event()
    notices = []
    peer = LateStopPeer(entered)
    following = Peer(names=("search",))
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)

    async def approval(ctx: ExecutorContext[None], arguments: Any) -> bool | StopTurn:
        notices.append(ctx)
        entered.set()
        await decide.wait()
        return StopTurn("private-terminal-child-stop")

    async with (
        timeout(40),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        following.serve() as url,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = Agent(
            instructions="Use children and selected tools.",
            model="gpt-5.5",
            provider=peer.provider(websocket=False),
            programmatic_tool_calling=True,
            tools=[
                HttpMCP(name="following", url=url, tools=["search"]),
                NativeAgents(
                    additional_roles=[
                        NativeRole(
                            name="research", description="Read a record", instructions="Read."
                        )
                    ]
                ),
            ],
        )
        async with agent.session(environment=declaration(executor, approval=approval)) as session:
            barrier = CleanupBarrier(session._native, cells=True)
            session._native = barrier
            root = await session.submit("Use both children.")
            assert (await root.get_result()).final_output == "root complete"
            await entered.wait()
            [ctx] = notices
            first = await handles(session, 2)
            alpha = next(t for t in first if t.child_id == ctx.thread_id)
            beta = next(t for t in first if t is not alpha)
            assert (await beta.get_result()).final_output == "beta complete"
            waiter = asyncio.create_task(alpha.get_result())
            try:
                state = next(
                    s
                    for s in session._child_callbacks.values()
                    if s.handle.child_id == alpha.child_id and s.handle.turn_id == alpha.turn_id
                )
                # A native reader receipt, not a fabricated event or timeout.
                await asyncio.shield(state.terminal)
                assert not waiter.done()
                assert code_output(peer.child_requests["alpha"][-1], "shared-call-id").startswith(
                    "Script running with cell ID "
                )
                assert [c["arguments"]["key"] for c in following.calls] == ["surviving-beta-effect"]
                decide.set()
                await barrier.cells_entered.wait()
                assert not waiter.done(), "Turn.wait must join the stopped cells' real receipt"
                assert (await root.get_result()).final_output == "root complete"
                assert (await beta.get_result()).final_output == "beta complete"

                # Passive wait cancellation cannot withdraw owned termination.
                waiter.cancel()
                waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiter
                pending = asyncio.create_task(alpha.get_result())
                await asyncio.sleep(0)
                assert not pending.done()
                barrier.cells_release.set()
                with pytest.raises(NativeError) as error:
                    await pending
                assert not (executor.root / "must-not-exist").exists()
                assert [c["arguments"]["key"] for c in following.calls] == ["surviving-beta-effect"]
                assert len(peer.child_requests["alpha"]) == 2
                assert str(error.value) == "Turn stopped by policy"
                assert "private-terminal-child-stop" not in json.dumps(peer.requests)
                assert (await root.get_result()).final_output == "root complete"
                assert (await beta.get_result()).final_output == "beta complete"
            finally:
                decide.set()
                barrier.cells_release.set()
                await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.parametrize("observe", [True, False], ids=["waited-child", "unobserved-child"])
async def test_failed_child_retirement_remains_visible_after_settlement(
    tmp_path, monkeypatch, observe
):
    """An admitted policy stop does not hide a failed native retirement receipt."""
    entered, decide = asyncio.Event(), asyncio.Event()
    peer = LateStopPeer(entered)
    following = Peer(names=("search",))
    notices = []
    retirement_error = OSError("controlled native retirement failure")
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)

    class RetirementFailure:
        def __init__(self, native):
            self.native = native
            self.called = asyncio.Event()
            self.child = None

        def __getattr__(self, name):
            return getattr(self.native, name)

        async def _interrupt_code_mode_cells(self, *, stopped_only=False, child=None):
            if child is not None:
                assert stopped_only
                self.child = child
                self.called.set()
                raise retirement_error
            return await self.native._interrupt_code_mode_cells(stopped_only=stopped_only)

    async def approval(ctx, arguments):
        notices.append(ctx)
        entered.set()
        await decide.wait()
        return StopTurn("private reason must not replace a failed receipt")

    async with (
        timeout(45),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        following.serve() as url,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = Agent(
            instructions="Use children and selected tools.",
            model="gpt-5.5",
            provider=peer.provider(websocket=False),
            programmatic_tool_calling=True,
            tools=[
                HttpMCP(name="following", url=url, tools=["search"]),
                NativeAgents(
                    additional_roles=[
                        NativeRole(name="research", description="Read", instructions="Read.")
                    ]
                ),
            ],
        )
        with pytest.raises(OSError) as exit_error:
            async with agent.session(
                environment=declaration(executor, approval=approval)
            ) as session:
                port = RetirementFailure(session._native)
                session._native = port
                root = await session.submit("Use both children.")
                assert (await root.get_result()).final_output == "root complete"
                await entered.wait()
                [ctx] = notices
                first = await handles(session, 2)
                alpha = next(t for t in first if t.child_id == ctx.thread_id)
                beta = next(t for t in first if t is not alpha)
                children = [child async for child in session.subagents()]
                sibling = next(child for child in children if child.id == beta.child_id)
                assert (await beta.get_result()).final_output == "beta complete"
                state = next(
                    s
                    for s in session._child_callbacks.values()
                    if s.handle.child_id == alpha.child_id and s.handle.turn_id == alpha.turn_id
                )
                # Observe actual native terminal before allowing the held policy.
                # The private completion makes the otherwise unobserved failure
                # deterministic before Session exit; it does not fake a result.
                await asyncio.shield(state.terminal)
                decide.set()
                await port.called.wait()
                await asyncio.shield(state.completion)
                assert not state.busy
                assert port.child == (alpha.child_id, state.native_instance, alpha.turn_id)
                if observe:
                    for _ in range(2):
                        with pytest.raises(NativeError, match="^Turn stopped by policy$") as failed:
                            await alpha.get_result()
                        assert failed.value.__context__ is retirement_error
                        assert retirement_error.__context__ is None
                assert (await root.get_result()).final_output == "root complete"
                assert (await beta.get_result()).final_output == "beta complete"
                assert [call["arguments"]["key"] for call in following.calls] == [
                    "surviving-beta-effect"
                ]
                request_count = len(peer.requests)
                with pytest.raises(RuntimeError, match="unconfirmed native operation"):
                    await session.submit("Must not admit root work after uncertain retirement.")
                with pytest.raises(RuntimeError, match="unconfirmed native operation"):
                    await sibling.start("Must not admit sibling work after uncertain retirement.")
                assert len(peer.requests) == request_count
        assert exit_error.value is retirement_error
