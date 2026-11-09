"""Exercise public one-shot ownership with real HTTP and a shared Provider."""

from __future__ import annotations

import asyncio
import gc
import importlib
import json
import weakref
from typing import Any

import pytest

from agents import Agent
from agents.observation import EventGap, OutputTextDelta, TurnFinished
from tests._asyncio import timeout
from tests.native_fixtures import text_response
from tests.test_native_agent_observation import collect
from tests.test_sdk_session_transport import disconnected, http_peer


@pytest.fixture
def prepared_runs(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    """Observe candidate admission without retaining or replacing native owners."""
    native = importlib.import_module("agents._native")
    prepare = native._prepare_run_conversation
    calls: list[None] = []

    def record(*args: Any, **kwargs: Any) -> Any:
        calls.append(None)
        return prepare(*args, **kwargs)

    monkeypatch.setattr(native, "_prepare_run_conversation", record)
    return calls


async def test_cancelled_public_run_joins_before_shared_provider_reuse(
    prepared_runs: list[None],
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_cancelled_delta(event: dict[str, Any]) -> None:
        if event.get("type") == "response.output_text.delta" and event.get("item_id") == "blocked":
            entered.set()
            await release.wait()

    async with timeout(30), http_peer() as peer, peer.provider() as shared:
        peer.before_event = hold_cancelled_delta
        peer.respond(*text_response("blocked", "unaccepted answer"))
        agent = Agent(model="gpt-5.2", instructions="Answer briefly.", provider=shared)
        running = asyncio.create_task(agent.run("cancelled input"))
        try:
            await entered.wait()
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            # The handler is still held. TestServer has not closed the client.
            await disconnected(peer.transports[:1])
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

        peer.respond(*text_response("recovered", "fresh answer"))
        recovered = await agent.run("fresh input")
        assert recovered.final_output == "fresh answer"
        assert len(peer.requests) == 2
        assert len(prepared_runs) == 2
        recovery_input = json.dumps(peer.requests[1]["input"])
        assert "fresh input" in recovery_input
        assert "cancelled input" not in recovery_input
        assert "unaccepted answer" not in recovery_input


async def test_cancelling_one_public_run_leaves_concurrent_shared_provider_run_alive(
    prepared_runs: list[None],
) -> None:
    cancelled_entered = asyncio.Event()
    survivor_entered = asyncio.Event()
    release_cancelled = asyncio.Event()
    release_survivor = asyncio.Event()

    async def hold_each_delta(event: dict[str, Any]) -> None:
        if event.get("type") != "response.output_text.delta":
            return
        if event.get("item_id") == "cancelled-root":
            cancelled_entered.set()
            await release_cancelled.wait()
        elif event.get("item_id") == "surviving-root":
            survivor_entered.set()
            await release_survivor.wait()

    async with timeout(30), http_peer() as peer, peer.provider() as shared:
        peer.before_event = hold_each_delta
        peer.respond(*text_response("cancelled-root", "cancelled answer"))
        peer.respond(*text_response("surviving-root", "surviving answer"))
        agent = Agent(model="gpt-5.2", instructions="Answer briefly.", provider=shared)
        cancelled = asyncio.create_task(agent.run("cancel this root"))
        survivor = None
        try:
            # Fix response assignment without serializing the two executions.
            await cancelled_entered.wait()
            survivor = asyncio.create_task(agent.run("keep this root"))
            await survivor_entered.wait()
            assert len(peer.transports) == 2
            assert peer.transports[0] is not peer.transports[1]

            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled
            await disconnected(peer.transports[:1])
            assert not survivor.done()
            assert not peer.transports[1].is_closing()

            release_survivor.set()
            completed = await survivor
            assert completed.final_output == "surviving answer"
            assert len(peer.requests) == 2
            assert len(prepared_runs) == 2
            survivor_input = json.dumps(peer.requests[1]["input"])
            assert "keep this root" in survivor_input
            assert "cancel this root" not in survivor_input
            assert "cancelled answer" not in survivor_input
        finally:
            release_cancelled.set()
            release_survivor.set()
            pending = [cancelled] if survivor is None else [cancelled, survivor]
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)


async def test_held_completed_runs_replay_after_provider_close_and_are_collectable(
    prepared_runs: list[None],
) -> None:
    async with timeout(30), http_peer() as peer:
        identities: set[str] = set()
        for batch in range(2):
            async with peer.provider() as shared:
                agent = Agent(model="gpt-5.2", instructions="Answer briefly.", provider=shared)
                held = []
                for index in range(3):
                    expected = f"retained {batch}:{index} café 漢🙂"
                    peer.respond(*text_response(f"held-{batch}-{index}", expected))
                    completed = await agent.run(f"fresh {batch}:{index}")
                    assert completed.final_output == expected
                    assert completed.turn.session_id not in identities
                    identities.add(completed.turn.session_id)
                    held.append(completed)
                del completed

            # Keep every result alive until the actual Provider has closed.
            await disconnected(peer.transports)
            references = [weakref.ref(completed) for completed in held]
            for index, completed in enumerate(held):
                expected = f"retained {batch}:{index} café 漢🙂"
                assert completed.final_output == expected
                assert (await completed.turn.get_result()).final_output == expected
                events = await collect(completed.turn.events())
                assert not any(isinstance(event, EventGap) for event in events)
                assert (
                    "".join(event.delta for event in events if isinstance(event, OutputTextDelta))
                    == expected
                )
                assert isinstance(events[-1], TurnFinished)
                assert events[-1].status == "completed"
            del completed, events, agent
            held.clear()
            # Allow result-waiter callbacks to release their references first.
            await asyncio.sleep(0)
            gc.collect()
            assert all(reference() is None for reference in references)

        assert len(peer.requests) == 6
        assert len(prepared_runs) == 6
