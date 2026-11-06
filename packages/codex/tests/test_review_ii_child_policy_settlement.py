"""Graph interruption joins policies registered while a child drains native work."""

from __future__ import annotations

import asyncio
import threading
import traceback
from collections import deque
from contextlib import AsyncExitStack
from typing import Any, Literal

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, NativeAgents, NativeError, NativeToolCall, ToolContext, function_tool
from agents.executor import ExecutorContext
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output
from tests.test_native_child_agents import collaboration, handles, trust
from tests.test_native_code_mode import code_call
from tests.test_native_executor_tools import declaration
from tests.test_review_ii_code_cell_interrupt import CellGraph


@pytest.mark.parametrize(
    ("policy", "kind", "ending", "fails"),
    [
        ("approval", "sync", "force", False),
        ("approval", "async", "close", False),
        ("approval", "sync", "force", True),
        ("approval", "async", "force", True),
        ("approval", "sync", "close", True),
        ("approval", "async", "close", True),
        ("transform_arguments", "sync", "force", True),
        ("transform_arguments", "async", "force", True),
        ("transform_arguments", "sync", "close", True),
        ("transform_arguments", "async", "close", True),
    ],
)
async def test_stop_joins_environment_policy_registered_during_child_callback_drain(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    policy: Literal["approval", "transform_arguments"],
    kind: Literal["sync", "async"],
    ending: Literal["force", "close"],
    fails: bool,
) -> None:
    gate_entered = asyncio.Event()
    gate_release = asyncio.Event()
    marker_entered = asyncio.Event()
    marker_release = asyncio.Event()
    policy_entered = asyncio.Event()
    policy_release = threading.Event()
    async_release = asyncio.Event()
    policy_exited = threading.Event()
    loop = asyncio.get_running_loop()

    @function_tool
    async def gate() -> str:
        """Wait for the application's background-work signal."""
        gate_entered.set()
        await gate_release.wait()
        return "continue"

    @function_tool
    async def marker() -> str:
        """Finish the child's parallel record after application confirmation."""
        marker_entered.set()
        await marker_release.wait()
        return "recorded"

    def run_sync(ctx: ExecutorContext[None], value: Any) -> Any:
        assert ctx.tool_name == "exec_command"
        assert "late-child-policy-7a18" in value["cmd"]
        loop.call_soon_threadsafe(policy_entered.set)
        try:
            policy_release.wait()
            if fails:
                raise RuntimeError("private-policy-cleanup-46fd")
            return True if policy == "approval" else value
        finally:
            policy_exited.set()

    async def run_async(ctx: ExecutorContext[None], value: Any) -> Any:
        assert ctx.tool_name == "exec_command"
        assert "late-child-policy-7a18" in value["cmd"]
        policy_entered.set()
        try:
            await asyncio.Future()
        finally:
            try:
                await async_release.wait()
                if fails:
                    raise RuntimeError("private-policy-cleanup-46fd")
            finally:
                policy_exited.set()

    peer = CellGraph()
    peer.before_event = peer.hold
    idle = assistant_output("alpha idle")
    idle["item"]["id"] = "done-alpha"
    peer.programs["/root/alpha"] = deque(
        [
            [
                code_call(
                    "yield_control(); await Promise.all([tools.marker({}), (async () => {"
                    "await tools.gate({}); text(await tools.exec_command("
                    "{cmd:'printf forbidden > late-child-policy-7a18'}));})()]);",
                    "background-alpha",
                )
            ],
            [idle],
        ]
    )
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    async with (
        timeout(55),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = Agent(
            instructions="Start the requested background program.",
            model="gpt-6.1-sol",
            provider=peer.provider(websocket=False),
            tools=[gate, marker, NativeAgents(max_children=2)],
            programmatic_tool_calling=True,
        )
        async with AsyncExitStack() as sessions:
            session = await sessions.enter_async_context(
                agent.session(
                    environment=declaration(
                        executor,
                        **{policy: run_sync if kind == "sync" else run_async},
                    )
                )
            )
            stop = None
            cancelling = None
            try:
                root = await session.submit("Start root code and an alpha child")
                assert (await root.get_result()).final_output == "root idle"
                [child] = await handles(session, 1)
                await asyncio.gather(gate_entered.wait(), marker_entered.wait())
                peer.release["alpha"].set()
                # The child has finished native inference, but the yielded
                # code cell still owns two pending automatic functions.
                [owner] = session._callbacks_for_child(child)
                await asyncio.shield(owner.terminal)
                assert len(peer.by_thread["/root/alpha"]) == 2
                gate_release.set()
                await policy_entered.wait()
                assert not (executor.root / "late-child-policy-7a18").exists()
                marker_release.set()
                # A separate completed root turn lets the former child drain
                # run without relying on a sleep for its scheduling.
                peer.programs["/root"].append([assistant_output("barrier two")])
                assert (
                    await (await session.submit("Leave child work running")).get_result()
                ).final_output == ("barrier two")
                if ending == "force":
                    cancelling = asyncio.create_task(session.cancel_all())
                    await asyncio.sleep(0)
                    assert not policy_exited.is_set()
                stop = asyncio.create_task(sessions.aclose())
                # Close must remain pending while the policy owns a worker.
                done, _ = await asyncio.wait((stop,), timeout=0.1)
                assert not done, "close returned before the policy worker exited"
                assert not policy_exited.is_set()
                policy_release.set()
                async_release.set()
                if fails:
                    with pytest.raises(NativeError, match="cleanup") as caught:
                        await stop
                    assert "private-policy-cleanup-46fd" not in "".join(
                        traceback.format_exception(caught.value)
                    )
                else:
                    await stop
                if cancelling is not None:
                    await asyncio.gather(cancelling, return_exceptions=True)
                assert policy_exited.is_set()
                assert "private-policy-cleanup-46fd" not in caplog.text
                assert not (executor.root / "late-child-policy-7a18").exists()
                assert not any(
                    item.get("method") == "process/start" and "late-child-policy-7a18" in str(item)
                    for item in executor.requests
                )
            finally:
                peer.release["alpha"].set()
                gate_release.set()
                marker_release.set()
                policy_release.set()
                async_release.set()
                if stop is not None:
                    await asyncio.gather(stop, return_exceptions=True)


async def test_graph_stop_joins_descendant_collaboration_approval(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    release = threading.Event()
    exited = threading.Event()
    loop = asyncio.get_running_loop()

    def approve(context: ToolContext[None], call: NativeToolCall) -> bool:
        assert context.call_id == call.call_id
        if call.call_id == "spawn-beta":
            loop.call_soon_threadsafe(entered.set)
            try:
                release.wait()
            finally:
                exited.set()
        return True

    peer = CellGraph()
    peer.programs["/root"] = deque(
        [
            [
                collaboration(
                    "spawn_agent",
                    {
                        "task_name": "alpha",
                        "message": "Start a beta child.",
                        "agent_type": "default",
                        "fork_turns": "none",
                    },
                    "spawn-alpha",
                )
            ],
            [assistant_output("root idle")],
        ]
    )
    peer.programs["/root/alpha"] = deque(
        [
            [
                collaboration(
                    "spawn_agent",
                    {
                        "task_name": "beta",
                        "message": "Stay idle.",
                        "agent_type": "default",
                        "fork_turns": "none",
                    },
                    "spawn-beta",
                )
            ],
            [assistant_output("alpha idle")],
        ]
    )
    peer.programs["/root/alpha/beta"] = deque([[assistant_output("beta started")]])
    peer.by_thread["/root/alpha/beta"] = []
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    async with (
        timeout(35),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = Agent(
            instructions="Delegate according to the request.",
            model="gpt-6.1-sol",
            provider=peer.provider(websocket=False),
            tools=[
                NativeAgents(
                    max_children=2,
                    max_depth=2,
                    approval=approve,
                )
            ],
        )
        async with agent.session(environment=declaration(executor)) as session:
            stop = None
            try:
                root = await session.submit("Start alpha.")
                assert (await root.get_result()).final_output == "root idle"
                await entered.wait()
                assert not peer.by_thread["/root/alpha/beta"]
                stop = asyncio.create_task(session.cancel_all())
                done, _ = await asyncio.wait((stop,), timeout=0.1)
                assert not done, "cleanup finished before descendant approval exited"
                assert not exited.is_set()
                release.set()
                await stop
                assert exited.is_set()
                assert not peer.by_thread["/root/alpha/beta"]
                children = [child async for child in session.subagents()]
                assert len(children) == 1
            finally:
                release.set()
                if stop is not None:
                    await asyncio.gather(stop, return_exceptions=True)
