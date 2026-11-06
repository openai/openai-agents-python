"""Accepted native stops retain exact owners until their cleanup has joined."""

from __future__ import annotations

import asyncio
import json
import threading

import pytest
from aiohttp.test_utils import TestServer
from pydantic import BaseModel, model_serializer

from agents import NativeError, TurnStoppedError, function_tool
from agents.observation import ToolFinished, TurnFinished
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.test_native_child_agents import handles, trust
from tests.test_native_child_manual import declaration_for
from tests.test_native_executor_tools import declaration
from tests.test_review_ii_graph_interrupt import InterruptedGraph


async def test_all_force_at_acceptance_joins_workers_and_keeps_late_values_out(
    tmp_path, monkeypatch
):
    """Return root sync work at public acceptance, hold the exact child's worker."""
    loop = asyncio.get_running_loop()
    entered = {key: asyncio.Event() for key in ("root", "alpha")}
    released = {key: threading.Event() for key in entered}
    cleaned = {key: threading.Event() for key in entered}
    effects = []
    serialized = []
    late = "late-graph-private-result-c2b0"

    class LateValue(BaseModel):
        key: str

        @model_serializer
        def serialize(self) -> str:
            serialized.append(self.key)
            return late + ":" + self.key

    @function_tool
    def lookup(key: str) -> object:
        effects.append(("entered", key, threading.get_ident()))
        loop.call_soon_threadsafe(entered[key].set)
        try:
            if not released[key].wait(25):
                raise TimeoutError("Test did not release the synchronous graph worker")
            effects.append(("returned", key, threading.get_ident()))
            return LateValue(key=key)
        finally:
            cleaned[key].set()

    peer = InterruptedGraph(hold_root=True, roles=("default",))
    async with (
        timeout(55),
        TestServer(peer.application(), shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        async with declaration_for(peer, executor, lookup).session(
            environment=declaration(executor)
        ) as session:
            root = await session.submit("Start alpha, then each lookup a record")
            root_waiter = asyncio.create_task(root.get_result())
            child_waiter = None
            stopping = None
            try:
                await asyncio.gather(*(event.wait() for event in entered.values()))
                [child_turn] = await handles(session, 1)
                [child] = [item async for item in session.subagents()]
                assert child.is_live
                assert child.id == peer.child_metadata["alpha"][0]["thread_id"]
                assert child_turn.child_id == child.id
                child_waiter = asyncio.create_task(child_turn.get_result())
                assert sorted((stage, key) for stage, key, _ in effects) == [
                    ("entered", "alpha"),
                    ("entered", "root"),
                ]
                assert all(ident != threading.get_ident() for _, _, ident in effects)

                accepted = asyncio.Event()
                native = session._native

                class ObserveNativeAdmission:
                    def __getattr__(self, name):
                        return getattr(native, name)

                    def _interrupt_graph(self, mode):
                        settlement = native._interrupt_graph(mode)
                        accepted.set()
                        return settlement

                session._native = ObserveNativeAdmission()
                stopping = asyncio.create_task(session.cancel_all(mode="force"))
                await accepted.wait()
                # Acceptance is the contract boundary. Do not wait for Python
                # state.stopping: that would hide the native retirement race.
                assert not stopping.done()
                released["root"].set()
                with pytest.raises(TurnStoppedError):
                    await root_waiter
                assert cleaned["root"].is_set()
                assert not cleaned["alpha"].is_set()
                assert not child_waiter.done()
                assert session._graph_interrupts
                assert any(not owner.done() for owner in session._graph_interrupts)
                assert serialized == []
                assert len(peer.root_requests) == 2
                assert len(peer.child_requests["alpha"]) == 1
            finally:
                for event in released.values():
                    event.set()
                for event in peer.release.values():
                    event.set()
                await asyncio.gather(
                    *(task for task in (root_waiter, child_waiter, stopping) if task is not None),
                    return_exceptions=True,
                )
            with pytest.raises(TurnStoppedError):
                await root_waiter
            assert child_waiter is not None
            with pytest.raises(NativeError):
                await child_waiter
            assert stopping is not None
            await stopping
            assert not session._graph_interrupts
            assert all(event.is_set() for event in cleaned.values())
            assert sorted((stage, key) for stage, key, _ in effects) == [
                ("entered", "alpha"),
                ("entered", "root"),
                ("returned", "alpha"),
                ("returned", "root"),
            ]
            assert serialized == []
            assert late not in json.dumps(peer.requests)
            assert late not in str([item async for item in session.items()])
            assert late not in str([item async for item in session.items(subagent_id=child.id)])
            trace = [event async for event in root.events()]
            finished = [event for event in trace if isinstance(event, TurnFinished)]
            tool_finished = [
                event
                for event in trace
                if isinstance(event, ToolFinished) and event.name == "lookup"
            ]
            assert len(finished) == len(tool_finished) == 1
            assert finished[0].status == tool_finished[0].status == "cancelled"

            again = await child.start("explicit-alpha-restart")
            assert again.turn_id != child_turn.turn_id
            assert (await again.get_result()).final_output == "alpha complete"
            fresh = await session.submit("explicit-root-restart")
            assert fresh.turn_id != root.turn_id
            assert (await fresh.get_result()).final_output == "root complete"
            with pytest.raises(TurnStoppedError):
                await root.get_result()
            with pytest.raises(NativeError):
                await child_turn.get_result()
            assert (await fresh.get_result()).final_output == "root complete"
            assert (await again.get_result()).final_output == "alpha complete"
            assert len(peer.root_requests) == 3
            assert len(peer.child_requests["alpha"]) == 2
            assert serialized == []
            assert late not in json.dumps(peer.requests)
