"""Child cancellation joins exact native cells and cooperative policy cleanup."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from contextlib import nullcontext
from dataclasses import replace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    HttpMCP,
    MCPResultContext,
    NativeAgents,
    NativeError,
    NativeRole,
    ToolContext,
    TurnNeedsAttention,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output
from tests.native_vm_fixtures import state_transfer_gate
from tests.test_native_child_agents import ChildPeer, handles, metadata, trust
from tests.test_native_child_tool_integration import Memory, family_agent, family_peer
from tests.test_native_code_mode import code_call
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_tools import Peer
from tests.test_review_ii_code_cell_interrupt import CellGraph, inspect_and_reuse, yielded_cell


async def test_child_cancel_joins_yielded_cell_and_preserves_root_and_successor(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = CellGraph()
    peer.before_event = peer.hold
    peer.programs["/root/alpha"][-1][0]["item"]["id"] = "done-alpha"
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
            tools=[NativeAgents(max_children=2)],
            programmatic_tool_calling=True,
        )
        async with agent.session(environment=declaration(executor)) as session:
            try:
                assert (
                    await (await session.submit("Start background work")).get_result()
                ).final_output == "root idle"
                await peer.entered["alpha"].wait()
                [captured] = await handles(session, 1)
                [child] = [item async for item in session.subagents()]
                root_requests = peer.by_thread["/root"]
                child_requests = peer.by_thread["/root/alpha"]
                root_cell = yielded_cell(root_requests[3], "root")
                child_cell = yielded_cell(child_requests[2], "alpha")
                assert await child.try_cancel(captured) is True
                peer.release["alpha"].set()
                attention = await captured.wait_for_attention()
                assert isinstance(attention, TurnNeedsAttention)
                assert attention.reason == "interrupted"
                assert isinstance(attention.error, NativeError)
                await inspect_and_reuse(
                    child,
                    peer.programs["/root/alpha"],
                    child_requests,
                    "alpha",
                    child_cell,
                    stopped=True,
                )
                assert await child.try_cancel(captured) is False
                await inspect_and_reuse(
                    session, peer.programs["/root"], root_requests, "root", root_cell, stopped=False
                )
            finally:
                peer.release["alpha"].set()


class PolicyChild(ChildPeer):
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
            return [
                code_call(
                    "text(await tools.mcp__vault__find_record({key:'record'}));", "child-policy"
                )
            ]
        result = assistant_output(name + " complete")
        if name == "beta":
            result["item"]["id"] = "done-beta"
        return [result]


@pytest.mark.parametrize(
    "cleanup_fails,native_cleanup_fails,cancel_waiter,close_race",
    [(False, False, False, False), (True, False, False, False)]
    + [
        pytest.param(
            False,
            True,
            cancelled,
            closing,
            marks=pytest.mark.skipif(
                sys.platform != "linux" or not hasattr(os, "pidfd_open"),
                reason=(
                    "The transparent helper gate uses Linux executable pipes and process-exit "
                    "descriptors"
                ),
            ),
        )
        for cancelled in [False, True]
        for closing in [False, True]
    ],
)
async def test_child_cancel_joins_native_mcp_policy_cleanup_without_stopping_sibling(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_fails: bool,
    native_cleanup_fails: bool,
    cancel_waiter: bool,
    close_race: bool,
) -> None:
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    exited = asyncio.Event()

    async def transform(ctx: MCPResultContext[None], result: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()
            exited.set()
            if cleanup_fails:
                raise RuntimeError("private-cancel-cleanup-817a")

    peer = PolicyChild()
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    vault = Peer(names=("find-record",), label="private-child-policy-43b5")
    async with (
        timeout(55),
        TestServer(app, shutdown_timeout=1) as server,
        vault.serve() as url,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        (
            state_transfer_gate(tmp_path, monkeypatch, "terminate")
            if native_cleanup_fails
            else nullcontext()
        ) as gate,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent = Agent(
            instructions="Start alpha and beta.",
            model="gpt-6.1-sol",
            provider=peer.provider(websocket=False),
            tools=[
                HttpMCP(name="vault", url=url, tools=["find-record"], transform_result=transform),
                NativeAgents(
                    additional_roles=[
                        NativeRole(name="research", description="Read", instructions="Read once.")
                    ],
                    max_children=2,
                ),
            ],
            programmatic_tool_calling=True,
        )
        async with agent.session(environment=declaration(executor)) as session:
            pending = closing = None
            termination_released = False
            native_returned, return_receipt, close_owned = (asyncio.Event() for _ in range(3))
            actual = session._native

            class HeldReceipt:
                def __getattr__(self, name: str) -> Any:
                    return getattr(actual, name)

                async def _cancel_child_turn(self, handle: Any) -> bool | None:
                    try:
                        result = await actual._cancel_child_turn(handle)
                    finally:
                        native_returned.set()
                    if close_race:
                        await return_receipt.wait()
                    return result

                def _interrupt_graph(self, *args: Any) -> Any:
                    close_owned.set()
                    return actual._interrupt_graph(*args)

            if native_cleanup_fails:
                session._native = HeldReceipt()
            try:
                assert (
                    await (await session.submit("Start children")).get_result()
                ).final_output == "root complete"
                await entered.wait()
                await peer.entered["beta"].wait()
                turns = {turn.child_id: turn for turn in await handles(session, 2)}
                alpha_id = peer.child_metadata["alpha"][0]["thread_id"]
                beta_id = peer.child_metadata["beta"][0]["thread_id"]
                child = [item async for item in session.subagents() if item.id == alpha_id][0]
                pending = asyncio.create_task(child.try_cancel(turns[alpha_id]))
                await cleaning.wait()
                assert not pending.done() and not exited.is_set()
                peer.release["beta"].set()
                assert (await turns[beta_id].get_result()).final_output == "beta complete"
                if native_cleanup_fails:
                    await gate.wait("terminate_rejected")
                    await native_returned.wait()
                    await gate.release("terminate")
                    await gate.wait("terminate_released")
                    termination_released = True
                    if close_race:
                        closing = asyncio.create_task(session._aclose())
                        await close_owned.wait()
                    if cancel_waiter:
                        pending.cancel()
                    await asyncio.sleep(0)
                assert not pending.done()
                release.set()
                if closing is not None:
                    await closing
                    return_receipt.set()
                if cleanup_fails or native_cleanup_fails:
                    expected = asyncio.CancelledError if cancel_waiter else NativeError
                    with pytest.raises(expected) as failure:
                        await pending
                    if not cancel_waiter:
                        assert failure.value.args == ("Child cancellation cleanup failed",)
                    remaining, seen = [failure.value], set()
                    while remaining:
                        error = remaining.pop()
                        if id(error) in seen:
                            continue
                        seen.add(id(error))
                        for private in [
                            "private-cancel-cleanup-817a",
                            "private-native-termination-error",
                        ]:
                            assert private not in repr(error.args)
                            assert private not in "".join(traceback.format_exception(error))
                        remaining.extend(
                            item
                            for item in (error.__cause__, error.__context__)
                            if item is not None
                        )
                else:
                    assert await pending is True
                assert exited.is_set()
                if closing is None:
                    with pytest.raises(NativeError):
                        await turns[alpha_id].get_result()
                assert "private-child-policy-43b5" not in json.dumps(peer.requests)
                assert len(peer.child_requests["alpha"]) == 1
            finally:
                release.set()
                return_receipt.set()
                if gate is not None and not termination_released:
                    await gate.release("terminate")
                for event in peer.release.values():
                    event.set()
                await asyncio.gather(
                    *(task for task in (pending, closing) if task is not None),
                    return_exceptions=True,
                )
                if gate is not None:
                    await session._aclose()
                    await agent.provider.aclose()
                    await gate.wait_for_helper_exit()


@pytest.mark.parametrize("reader_exit", [False, True])
async def test_child_cancel_late_receipt_does_not_create_an_unsettled_owner(
    tmp_path, monkeypatch: pytest.MonkeyPatch, reader_exit: bool
) -> None:
    peer = CellGraph()
    peer.before_event = peer.hold
    peer.programs["/root/alpha"][-1][0]["item"]["id"] = "done-alpha"
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
            instructions="Start background work.",
            model="gpt-6.1-sol",
            provider=peer.provider(websocket=False),
            tools=[NativeAgents(max_children=2)],
            programmatic_tool_calling=True,
        )
        async with agent.session(environment=declaration(executor)) as session:
            delivered = asyncio.Event()
            read_events = asyncio.Event()
            confirmed = asyncio.Event()
            return_receipt = asyncio.Event()
            original_reader = session._read_child_event
            actual = session._native
            backlog: list[dict[str, Any]] = []

            async def held_event(event: dict[str, Any]) -> None:
                # Buffer real Python delivery while the native event reader
                # continues observing the exact turn and its terminal receipt.
                if not read_events.is_set():
                    backlog.append(event)
                    delivered.set()
                    return
                await original_reader(event)

            class HeldReceipt:
                def __getattr__(self, name: str) -> Any:
                    return getattr(actual, name)

                async def _cancel_child_turn(self, handle: Any) -> bool:
                    result = await actual._cancel_child_turn(handle)
                    assert result is True
                    confirmed.set()
                    await return_receipt.wait()
                    return result

            session._read_child_event = held_event
            session._native = HeldReceipt()
            pending = closing = None
            try:
                await session.submit("Start background work")
                await delivered.wait()
                await peer.entered["alpha"].wait()
                [child] = [item async for item in session.subagents()]
                assert child.turn is not None and not session._child_callbacks
                pending = asyncio.create_task(child.try_cancel(child.turn))
                await confirmed.wait()
                if reader_exit:
                    # Lose the event source after native cancellation settles but
                    # before its Python receipt can register a callback owner.
                    session._reader.cancel()
                    await session._reader
                    assert session._broken and not session._closing
                else:
                    closing = asyncio.create_task(session._aclose())
                    # _aclose seals before its first await; let the actual reader drain
                    # after close has claimed ownership, without delivering stale events.
                    await asyncio.sleep(0)
                    assert session._closing
                    read_events.set()
                    for event in backlog:
                        await original_reader(event)
                    backlog.clear()
                    await closing
                assert not session._child_callbacks
                return_receipt.set()
                done, _ = await asyncio.wait((pending,), timeout=5)
                assert pending in done, "Cancellation must not wait for retired child events"
                if reader_exit:
                    with pytest.raises(NativeError) as failure:
                        await pending
                    assert failure.value.args == ("Child cancellation cleanup failed",)
                    assert failure.value.__cause__ is None and failure.value.__context__ is None
                else:
                    assert await pending is True
                assert not session._child_callbacks
            finally:
                read_events.set()
                return_receipt.set()
                peer.release["alpha"].set()
                # A failing regression must still release an erroneously created
                # post-close settlement task, which has no remaining event source.
                if session._closed or session._reader.done():
                    for owner in session._child_callbacks.values():
                        if owner.task is not None and not owner.task.done():
                            owner.task.cancel()
                await asyncio.gather(
                    *(task for task in (pending, closing) if task is not None),
                    return_exceptions=True,
                )


async def test_unconfirmed_child_cancel_error_leaves_callback_owner_live(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release, cancelled, exited = (asyncio.Event() for _ in range(4))
    memory = Memory()

    @function_tool(name="lookup")
    async def lookup(ctx: ToolContext[Memory], key: str) -> str:
        if key == "alpha":
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            finally:
                exited.set()
        return "accepted:" + key

    vault = Peer(names=("find-record",), label="mcp-private-f126")
    async with (
        timeout(40),
        family_peer(programmatic_tool_calling=False) as peer,
        vault.serve() as url,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        base = family_agent(peer, executor, url, memory)
        agent = replace(base, tools=[lookup, *base.tools[1:]])
        async with agent.session(context=memory, environment=declaration(executor)) as session:
            actual = session._native
            failure = RuntimeError("Native cancellation did not confirm a decision")

            class Unconfirmed:
                def __getattr__(self, name: str) -> Any:
                    return getattr(actual, name)

                async def _cancel_child_turn(self, handle: Any) -> bool:
                    raise failure

            pending = None
            try:
                await (await session.submit("Start both children")).get_result()
                peer.release["alpha"].set()
                await entered.wait()
                alpha_id = peer.child_metadata["alpha"][0]["thread_id"]
                child = [item async for item in session.subagents() if item.id == alpha_id][0]
                assert child.turn is not None
                captured = child.turn
                session._native = Unconfirmed()
                pending = asyncio.create_task(child.try_cancel(captured))
                done, _ = await asyncio.wait((pending,), timeout=2)
                assert pending in done, "An unconfirmed error must not wait for callback cleanup"
                with pytest.raises(RuntimeError) as caught:
                    await pending
                assert caught.value is failure
                assert not cancelled.is_set() and not exited.is_set()
                session._native = actual
                release.set()
                assert (await captured.get_result()).final_output == "alpha complete"
                assert exited.is_set() and not cancelled.is_set()
            finally:
                session._native = actual
                release.set()
                for event in peer.release.values():
                    event.set()
                if pending is not None:
                    await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("cancel_waiter", [False, True])
@pytest.mark.parametrize("close_race", [False, True])
async def test_child_cancel_withholds_private_function_cleanup_exception_chains(
    tmp_path, monkeypatch: pytest.MonkeyPatch, cancel_waiter: bool, close_race: bool
) -> None:
    entered, cleaning, release, exited = (asyncio.Event() for _ in range(4))
    private = "private-child-cleanup-928f"
    memory = Memory()

    @function_tool(name="lookup")
    async def lookup(ctx: ToolContext[Memory], key: str) -> str:
        if key != "alpha":
            return "accepted:" + key
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release.wait()
            exited.set()
            try:
                raise ValueError(private, {"payload": private})
            except ValueError as cause:
                raise RuntimeError(private, [private]) from cause

    vault = Peer(names=("find-record",), label="synthetic-unused-vault")
    async with (
        timeout(55),
        family_peer(programmatic_tool_calling=False) as peer,
        vault.serve() as url,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        base = family_agent(peer, executor, url, memory)
        agent = replace(base, tools=[lookup, *base.tools[1:]])
        body_completed = False
        expected_close = pytest.raises(RuntimeError, match=private) if close_race else nullcontext()
        with expected_close:
            async with agent.session(context=memory, environment=declaration(executor)) as session:
                pending = closing = None
                confirmed, return_receipt, close_owned = (asyncio.Event() for _ in range(3))
                actual = session._native

                class HeldReceipt:
                    def __getattr__(self, name: str) -> Any:
                        return getattr(actual, name)

                    async def _cancel_child_turn(self, handle: Any) -> bool:
                        result = await actual._cancel_child_turn(handle)
                        assert result is True
                        confirmed.set()
                        await return_receipt.wait()
                        return result

                    def _interrupt_graph(self, *args: Any) -> Any:
                        # Close captures its callback owners before this call.
                        close_owned.set()
                        return actual._interrupt_graph(*args)

                if close_race:
                    session._native = HeldReceipt()
                try:
                    assert (
                        await (await session.submit("Start both children")).get_result()
                    ).final_output == "root complete"
                    peer.release["alpha"].set()
                    await entered.wait()
                    alpha_id = peer.child_metadata["alpha"][0]["thread_id"]
                    child = [item async for item in session.subagents() if item.id == alpha_id][0]
                    assert child.turn is not None
                    pending = asyncio.create_task(child.try_cancel(child.turn))
                    await cleaning.wait()
                    if close_race:
                        await confirmed.wait()
                        closing = asyncio.create_task(session._aclose())
                        await close_owned.wait()
                    if cancel_waiter:
                        pending.cancel()
                        await asyncio.sleep(0)
                    assert not pending.done() and not exited.is_set()
                    release.set()
                    if closing is not None:
                        # Session.close keeps its existing application error surface.
                        with pytest.raises(RuntimeError, match=private):
                            await closing
                        return_receipt.set()
                    expected = asyncio.CancelledError if cancel_waiter else NativeError
                    with pytest.raises(expected) as caught:
                        await pending
                    assert exited.is_set()
                    if not cancel_waiter:
                        assert caught.value.args == ("Child cancellation cleanup failed",)
                    remaining = [caught.value]
                    seen: set[int] = set()
                    while remaining:
                        error = remaining.pop()
                        if id(error) in seen:
                            continue
                        seen.add(id(error))
                        assert private not in repr(error.args)
                        assert private not in "".join(traceback.format_exception(error))
                        remaining.extend(
                            item
                            for item in (error.__cause__, error.__context__)
                            if item is not None
                        )
                    body_completed = True
                finally:
                    release.set()
                    return_receipt.set()
                    for event in peer.release.values():
                        event.set()
                    await asyncio.gather(
                        *(task for task in (pending, closing) if task is not None),
                        return_exceptions=True,
                    )
        assert body_completed, "Expected close failure must not mask a failed body assertion"
