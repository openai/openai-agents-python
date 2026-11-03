"""Exact root StopTurn ownership across overlapping cells and final settlement."""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing

import pytest

from agents import (
    Agent,
    HttpMCP,
    NativeError,
    StopTurn,
    ToolContext,
    TurnStoppedError,
    function_tool,
)
from agents.observation import TurnFinished
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_agent import provider
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_mcp_tools import Peer
from tests.test_native_stop_boundaries import CleanupBarrier


class UsageBarrier:
    """Delay only the SDK's final administrative usage read."""

    def __init__(self, native):
        self.native = native
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name):
        return getattr(self.native, name)

    async def call(self, operation, *args, **kwargs):
        if operation == "token_usage_info":
            self.entered.set()
            await self.release.wait()
        return await self.native.call(operation, *args, **kwargs)


@pytest.mark.parametrize("stop", [False, True], ids=["local-deny", "stop-turn"])
async def test_overlapping_mcp_response_cannot_start_native_or_python_effect_after_stop(
    responses: ResponsesFixture, stop
):
    gate_release = asyncio.Event()
    gate = Peer(names=("search",), release_call=gate_release)
    selected = Peer(names=("search",))
    following = Peer(names=("search",))
    entered, python_entered = asyncio.Event(), asyncio.Event()
    notices, managed = [], []

    @function_tool
    async def commit_receipt(context: ToolContext, key: str) -> str:
        """Record the actual managed application effect."""
        managed.append((context, key))
        python_entered.set()
        return "managed-receipt"

    async def approval(context, call):
        await gate.call_entered.wait()
        notices.append((context, call))
        entered.set()
        return StopTurn("private-overlap-stop-198a") if stop else False

    responses.respond(
        code_call(
            "const parallel = (async () => {"
            'await tools.mcp__gate__search({key:"already-started"});'
            "return await Promise.all(["
            'tools.mcp__following__search({key:"after-stop"}),'
            'tools.commit_receipt({key:"after-stop"})]); })();'
            'try { await tools.mcp__chosen__search({key:"selected"}); } catch (denial) {}'
            'await parallel; text("overlap-continued");',
            "overlap-cell",
        )
    )
    responses.respond(assistant_output("ordinary-denial-completed"))
    async with (
        timeout(30),
        gate.serve() as gate_url,
        selected.serve() as selected_url,
        following.serve() as following_url,
        aclosing(provider(responses)) as native_provider,
    ):
        async with Agent(
            instructions="Use tools.",
            tools=[
                commit_receipt,
                HttpMCP(name="gate", url=gate_url, tools=["search"]),
                HttpMCP(
                    name="chosen",
                    url=selected_url,
                    tools=["search"],
                    approval=approval,
                ),
                HttpMCP(name="following", url=following_url, tools=["search"]),
            ],
            provider=native_provider,
            programmatic_tool_calling=True,
        ).session() as session:
            barrier = CleanupBarrier(session._native, interrupt=True)
            session._native = barrier
            turn = await session.submit("Start the two ordinary promises.")
            waiter = asyncio.create_task(turn.get_result())
            try:
                await entered.wait()
                [(context, call)] = notices
                assert context.submission is turn
                assert call.thread_id == session.id and call.turn_id == turn.turn_id
                assert call.outer_call_id == "overlap-cell"
                assert call.cell_id and call.runtime_tool_call_id
                [gate_call] = gate.calls
                assert gate_call["name"] == "search"
                assert gate_call["arguments"] == {"key": "already-started"}
                assert gate_call["_meta"]["threadId"] == session.id
                assert selected.calls == following.calls == managed == []
                assert len(responses.requests) == 1
                if stop:
                    # This await is reached only after SDK handling of a real
                    # native before-decision. We never replace the claim.
                    await barrier.interrupt_entered.wait()
                    state = session._active
                    assert state is not None and state.handle is turn
                    assert str(state.failure.result()) == "Turn stopped by policy"
                    assert not waiter.done()
                gate_release.set()
                if stop:
                    # Let the already-issued request deliver its reply. While
                    # physical cleanup is held, watch for either forbidden new
                    # effect. The local-False control proves both real paths.
                    forbidden_native = asyncio.create_task(following.call_entered.wait())
                    forbidden_python = asyncio.create_task(python_entered.wait())
                    try:
                        await asyncio.wait(
                            (forbidden_native, forbidden_python),
                            timeout=1.0,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    finally:
                        forbidden_native.cancel()
                        forbidden_python.cancel()
                        await asyncio.gather(
                            forbidden_native, forbidden_python, return_exceptions=True
                        )
                    assert following.calls == [], (
                        "an unselected native effect started after StopTurn"
                    )
                    assert managed == [], "a managed Python effect started after StopTurn"
                    assert selected.calls == []
                    assert len(responses.requests) == 1
                    assert not waiter.done()
                else:
                    await following.call_entered.wait()
                    await python_entered.wait()
            finally:
                gate_release.set()
                barrier.interrupt_release.set()
                barrier.cells_release.set()
                await asyncio.gather(waiter, return_exceptions=True)
            assert selected.calls == []
            # StopTurn never attempts to undo an effect already in progress.
            assert len(gate.calls) == 1
            if stop:
                with pytest.raises(TurnStoppedError, match="^Turn stopped by policy$"):
                    await waiter
                assert following.calls == managed == []
                assert len(responses.requests) == 1
            else:
                assert (await waiter).final_output == "ordinary-denial-completed"
                [following_call] = following.calls
                assert following_call["name"] == "search"
                assert following_call["arguments"] == {"key": "after-stop"}
                assert following_call["_meta"]["threadId"] == session.id
                [(managed_context, key)] = managed
                assert managed_context.submission is turn and key == "after-stop"
                assert len(responses.requests) == 2
                assert "overlap-continued" in code_output(responses.requests[1], "overlap-cell")
            assert "private-overlap-stop-198a" not in json.dumps(responses.requests)


async def test_admitted_stop_after_local_deny_during_usage_cannot_publish_completed(
    responses: ResponsesFixture,
):
    first, first_release = asyncio.Event(), asyncio.Event()
    notices = []
    selected = Peer(names=("find-record",))
    terminal = assistant_output("native terminal while the yielded cell is live")

    async def approval(context, call):
        notices.append((context, call))
        if len(notices) == 1:
            first.set()
            await first_release.wait()
            return False
        return StopTurn("private-late-usage-stop-7d16")

    async def at_event(event):
        if event is terminal:
            await first.wait()

    responses.before_event = at_event
    responses.respond(
        code_call(
            'const pending = tools.mcp__vault__find_record({key:"first-local-deny"});'
            "yield_control();"
            "try { await pending; } catch (denial) {}"
            'try { await tools.mcp__vault__find_record({key:"second-stop"}); } catch (denial) {}'
            'text("late-usage-continuation");',
            "late-usage-cell",
        )
    )
    responses.respond(terminal)
    async with (
        timeout(30),
        selected.serve() as url,
        aclosing(provider(responses)) as native_provider,
    ):
        async with Agent(
            instructions="Use tools.",
            tools=[
                HttpMCP(
                    name="vault",
                    url=url,
                    tools=["find-record"],
                    approval=approval,
                )
            ],
            provider=native_provider,
            programmatic_tool_calling=True,
        ).session() as session:
            barrier = UsageBarrier(session._native)
            session._native = barrier
            turn = await session.submit("Yield the cell, then deny only its first leaf.")
            waiter = asyncio.create_task(turn.get_result())
            try:
                await first.wait()
                state = session._active
                assert state is not None and state.handle is turn
                await asyncio.shield(state.terminal)
                assert code_output(responses.requests[1], "late-usage-cell").startswith(
                    "Script running with cell ID "
                )
                assert not waiter.done()
                assert selected.calls == []
                first_release.set()
                await barrier.entered.wait()
                # A successful real native claim records the owning failure.
                # Observing it does not mock or replace the native admission.
                error = await asyncio.shield(state.failure)
                assert isinstance(error, NativeError) and str(error) == "Turn stopped by policy"
                assert len(notices) == 2
                assert all(context.submission is turn for context, _ in notices)
                assert all(
                    call.thread_id == session.id and call.turn_id == turn.turn_id
                    for _, call in notices
                )
                assert all(
                    call.outer_call_id == "late-usage-cell" and call.cell_id for _, call in notices
                )
                assert notices[0][1].runtime_tool_call_id != notices[1][1].runtime_tool_call_id
                assert selected.calls == [] and len(responses.requests) == 2
                assert not waiter.done()
            finally:
                first_release.set()
                barrier.release.set()
                await asyncio.gather(waiter, return_exceptions=True)
            with pytest.raises(TurnStoppedError, match="^Turn stopped by policy$") as caught:
                await waiter
            assert "private-late-usage-stop-7d16" not in str(caught.value.__context__)
            finished = [event async for event in turn.events() if isinstance(event, TurnFinished)]
            assert len(finished) == 1
            assert finished[0].turn_id == turn.turn_id and finished[0].status == "cancelled"
            assert finished[0].error_type == "TurnStoppedError"
            assert selected.calls == [] and len(responses.requests) == 2
            assert "private-late-usage-stop-7d16" not in json.dumps(responses.requests)
            # Re-reading the old handle must retain the same failure.
            with pytest.raises(TurnStoppedError, match="^Turn stopped by policy$"):
                await turn.get_result()
