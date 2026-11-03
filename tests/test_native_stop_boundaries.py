"""StopTurn at native deadlines, live yielded cells, and unsupported boundaries."""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing

import pytest

from agents import Agent, HttpMCP, NativeError, NativeProvider, StopTurn, TurnStoppedError
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_child_agents import agent_for, child_peer, handles, trust
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_stdio import records, stdio
from tests.test_native_mcp_tools import Peer


class CleanupBarrier:
    """Delay SDK administrative calls without replacing native authority or timers."""

    def __init__(self, native, *, interrupt=False, cells=False):
        self.native = native
        self.interrupt = interrupt
        self.cells = cells
        self.interrupt_entered = asyncio.Event()
        self.interrupt_release = asyncio.Event()
        self.cells_entered = asyncio.Event()
        self.cells_release = asyncio.Event()

    def __getattr__(self, name):
        return getattr(self.native, name)

    async def _interrupt_session(self, expected_turn_id):
        self.interrupt_entered.set()
        if self.interrupt:
            await self.interrupt_release.wait()
        return await self.native._interrupt_session(expected_turn_id)

    async def _interrupt_code_mode_cells(self, *args, **kwargs):
        self.cells_entered.set()
        if self.cells:
            await self.cells_release.wait()
        return await self.native._interrupt_code_mode_cells(*args, **kwargs)


@pytest.mark.requires_native_linux_sandbox
async def test_claimed_transform_stop_outlives_real_native_deadline(
    responses: ResponsesFixture, tmp_path, monkeypatch
):
    duration = 0.5
    decision_at = []
    notices = []
    following = Peer(names=("search",))

    async def before(context, value):
        notices.append((context, value))
        decision_at.append(asyncio.get_running_loop().time())
        return StopTurn("private-transform-stop-543a")

    responses.respond(
        code_call(
            'try { await tools.exec_command({cmd:"printf selected > selected-effect"}); }'
            "catch (denial) {}"
            'await tools.mcp__following__search({key:"following"}); text("continued");',
            "deadline-cell",
        )
    )
    responses.respond(assistant_output("unexpected-continuation"))
    async with (
        timeout(35),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        following.serve() as url,
        aclosing(
            NativeProvider(
                base_url=responses.url, api_key="synthetic-stop-deadline", supports_websockets=False
            )
        ) as provider,
    ):
        trust(executor, tmp_path, monkeypatch)
        async with Agent(
            instructions="Use tools.",
            tools=[HttpMCP(name="following", url=url, tools=["search"])],
            provider=provider,
            programmatic_tool_calling=True,
        ).session(
            environment=declaration(executor, transform_arguments=before, policy_timeout=duration)
        ) as session:
            barrier = CleanupBarrier(session._native, interrupt=True)
            session._native = barrier
            turn = await session.submit("start")
            waiter = asyncio.create_task(turn.get_result())
            try:
                await barrier.interrupt_entered.wait()
                [(context, supplied)] = notices
                assert context.submission is turn
                assert context.thread_id == session.id and context.turn_id == turn.turn_id
                assert context.outer_call_id == "deadline-cell"
                assert context.cell_id and context.runtime_tool_call_id
                assert supplied == {"cmd": "printf selected > selected-effect"}
                assert not (executor.root / "selected-effect").exists()
                assert following.calls == [] and len(responses.requests) == 1
                # The timer itself is the boundary under test. Time passes in
                # native Control as well as Python; no native deadline is mocked.
                delay = max(0, decision_at[0] + duration * 3 - asyncio.get_running_loop().time())
                await asyncio.sleep(delay)
                assert not waiter.done()
                assert not (executor.root / "selected-effect").exists()
                assert following.calls == []
                assert len(responses.requests) == 1
            finally:
                barrier.interrupt_release.set()
                barrier.cells_release.set()
                await asyncio.gather(waiter, return_exceptions=True)
            with pytest.raises(TurnStoppedError, match="^Turn stopped by policy$") as caught:
                await waiter
            assert "private-transform-stop-543a" not in str(caught.value.__context__)
            assert not (executor.root / "selected-effect").exists()
            assert following.calls == [] and len(responses.requests) == 1
            assert "private-transform-stop-543a" not in str(
                [item async for item in session.items()]
            )


@pytest.mark.requires_native_linux_sandbox
async def test_unclaimed_transform_deadline_remains_bounded_and_prevents_effect(
    responses: ResponsesFixture, tmp_path, monkeypatch
):
    entered, exited = asyncio.Event(), asyncio.Event()
    contexts = []

    async def before(context, value):
        contexts.append(context)
        entered.set()
        try:
            await asyncio.Future()
        finally:
            exited.set()

    responses.respond(
        code_call(
            'try { await tools.exec_command({cmd:"printf expired > expired-effect"}); }'
            'catch (denial) { text("ordinary expiry"); }',
            "unclaimed-cell",
        )
    )
    responses.respond(assistant_output("ordinary expiry handled"))
    async with (
        timeout(30),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        aclosing(
            NativeProvider(
                base_url=responses.url,
                api_key="synthetic-unclaimed-expiry",
                supports_websockets=False,
            )
        ) as provider,
    ):
        trust(executor, tmp_path, monkeypatch)
        async with Agent(
            instructions="Use tools.", provider=provider, programmatic_tool_calling=True
        ).session(
            environment=declaration(executor, transform_arguments=before, policy_timeout=0.5)
        ) as session:
            turn = await session.submit("let this decision expire")
            await entered.wait()
            assert len(contexts) == 1 and contexts[0].submission is turn
            assert contexts[0].outer_call_id == "unclaimed-cell"
            # Both existing deadline owners may win: the SDK reports its exact
            # policy failure; native expiry denies the leaf to generated code.
            # Neither path may accept StopTurn or an unrelated failure.
            async with timeout(8):
                try:
                    completed = await turn.get_result()
                except NativeError as error:
                    assert str(error) == "Executor policy failed"
                else:
                    assert completed.final_output == "ordinary expiry handled"
            assert exited.is_set()
            assert not (executor.root / "expired-effect").exists()
            assert len(responses.requests) <= 2


async def test_owned_mcp_stop_after_native_terminal_holds_until_yielded_cell_cleanup(
    responses: ResponsesFixture,
):
    entered, decide = asyncio.Event(), asyncio.Event()
    notices = []
    selected = Peer(names=("find-record",))
    following = Peer(names=("search",))

    async def approval(context, call):
        notices.append((context, call))
        entered.set()
        await decide.wait()
        return StopTurn("private-terminal-stop-b011")

    premature = assistant_output("native terminal while callback remains live")

    async def at_event(event):
        if event is premature:
            await entered.wait()

    responses.before_event = at_event
    responses.respond(
        code_call(
            'const pending = tools.mcp__vault__find_record({key:"selected"});'
            "yield_control();"
            "try { await pending; } catch (denial) {}"
            'await tools.mcp__following__search({key:"following"}); text("late-continuation");',
            "early-cell",
        )
    )
    responses.respond(premature)
    async with (
        timeout(35),
        selected.serve() as selected_url,
        following.serve() as following_url,
        aclosing(
            NativeProvider(
                base_url=responses.url, api_key="synthetic-terminal-stop", supports_websockets=False
            )
        ) as provider,
    ):
        agent = Agent(
            instructions="Use selected tools.",
            tools=[
                HttpMCP(
                    name="vault",
                    url=selected_url,
                    tools=["find-record"],
                    approval=approval,
                ),
                HttpMCP(name="following", url=following_url, tools=["search"]),
            ],
            provider=provider,
            programmatic_tool_calling=True,
        )
        async with agent.session() as session, agent.session() as other:
            barrier = CleanupBarrier(session._native, cells=True)
            session._native = barrier
            old = await session.submit("yield while selected call is pending")
            waiter = asyncio.create_task(old.get_result())
            try:
                await entered.wait()
                state = session._active
                assert state is not None and state.handle is old
                # Observe the real native reader receipt. Never synthesize
                # terminal state or replace admission/cancellation methods.
                await asyncio.shield(state.terminal)
                [(context, call)] = notices
                assert context.submission is old
                assert call.thread_id == session.id and call.turn_id == old.turn_id
                assert call.outer_call_id == "early-cell"
                assert call.cell_id and call.runtime_tool_call_id
                assert code_output(responses.requests[1], "early-cell").startswith(
                    "Script running with cell ID "
                )
                assert selected.calls == following.calls == []
                assert not waiter.done() and any(not task.done() for task in state.callbacks)

                responses.respond(assistant_output("foreign-survived"))
                foreign = await other.submit("independent session")
                assert (await foreign.get_result()).final_output == "foreign-survived"
                decide.set()
                await barrier.cells_entered.wait()
                assert session._active is state
                assert not waiter.done()
                # The policy callback may already have exited. The owned cell
                # cleanup must still hold the turn's result and active slot.
                assert state.policy_stop and state.stopping and not state.completion.done()
                assert selected.calls == following.calls == []
                assert len(responses.requests) == 3
                assert (await foreign.get_result()).final_output == "foreign-survived"
            finally:
                decide.set()
                barrier.interrupt_release.set()
                barrier.cells_release.set()
                await asyncio.gather(waiter, return_exceptions=True)
            with pytest.raises(TurnStoppedError, match="^Turn stopped by policy$"):
                await waiter
            assert selected.calls == following.calls == []
            assert len(responses.requests) == 3
            responses.respond(assistant_output("successor-survived"))
            successor = await session.submit("fresh explicit input")
            assert successor.turn_id != old.turn_id
            assert (await successor.get_result()).final_output == "successor-survived"
            with pytest.raises(TurnStoppedError, match="^Turn stopped by policy$"):
                await old.get_result()
            assert (await successor.get_result()).final_output == "successor-survived"
            assert (await foreign.get_result()).final_output == "foreign-survived"
            assert "private-terminal-stop-b011" not in json.dumps(responses.requests)


def test_stdio_policy_declaration_does_not_start_process_or_call_approval(tmp_path):
    log = tmp_path / "stdio-log"
    calls = []

    def approval(context, call):
        calls.append(call)
        return StopTurn("not reachable")

    stdio(log, approval=approval)
    assert calls == [] and records(log) == []


@pytest.mark.requires_native_linux_sandbox
async def test_child_stop_retires_exact_child_while_bool_sibling_and_root_survive(
    tmp_path, monkeypatch
):
    entered = {name: asyncio.Event() for name in ("alpha", "beta")}
    release = {name: asyncio.Event() for name in entered}
    notices = {}

    async def approval(context, value):
        name = "alpha" if "alpha.txt" in value["cmd"] else "beta"
        notices[name] = (context, value)
        entered[name].set()
        await release[name].wait()
        return StopTurn("private-child-stop-reason") if name == "alpha" else True

    async with (
        timeout(55),
        child_peer(("default", "default")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        async with agent_for(peer, executor).session(
            environment=declaration(executor, approval=approval)
        ) as session:
            root = await session.submit("start both children")
            try:
                assert (await root.get_result()).final_output == "root complete"
                await asyncio.gather(*(event.wait() for event in entered.values()))
                turns = {turn.child_id: turn for turn in await handles(session, 2)}
                alpha = turns[peer.child_metadata["alpha"][0]["thread_id"]]
                beta = turns[peer.child_metadata["beta"][0]["thread_id"]]
                assert notices["alpha"][0].thread_id == alpha.child_id
                assert notices["alpha"][0].turn_id == alpha.turn_id
                assert notices["beta"][0].thread_id == beta.child_id
                assert notices["beta"][0].turn_id == beta.turn_id
                assert not (executor.root / "alpha.txt").exists()
                assert not (executor.root / "beta.txt").exists()
                release["alpha"].set()
                with pytest.raises(NativeError, match="^Turn stopped by policy$") as caught:
                    await alpha.get_result()
                assert "private-child-stop-reason" not in str(caught.value.__context__)
                assert not (executor.root / "alpha.txt").exists()
                assert not (executor.root / "beta.txt").exists()
                assert (await root.get_result()).final_output == "root complete"
                release["beta"].set()
                await peer.entered["beta"].wait()
                assert (executor.root / "beta.txt").read_text() == "beta"
                peer.release["beta"].set()
                assert (await beta.get_result()).final_output == "beta complete"
                assert len(peer.child_requests["alpha"]) == 1
                assert (await root.get_result()).final_output == "root complete"
                assert (await beta.get_result()).final_output == "beta complete"
                with pytest.raises(NativeError, match="^Turn stopped by policy$"):
                    await alpha.get_result()
                assert not (executor.root / "alpha.txt").exists()
                assert "private-child-stop-reason" not in json.dumps(peer.requests)
            finally:
                for event in release.values():
                    event.set()
                for event in peer.release.values():
                    event.set()
