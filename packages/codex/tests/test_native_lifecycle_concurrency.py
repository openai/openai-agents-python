"""Ordinary reader activation preserves shared atomic opening and cancellation isolation."""

import asyncio
import importlib
import json

import pytest

from agents import Agent, NativeProvider, Session
from tests._asyncio import timeout
from tests.test_buffered_responses import json_peer, json_response
from tests.test_native_lifecycle_run import (
    atomic_opening_placement,
    exercise_shared_atomic_opening as exercise_shared_atomic_opening,
)
from tests.test_native_plain_event_collection import instrument_reader


async def test_cancelling_one_ordinary_reader_preserves_another_fresh_conversation(monkeypatch):
    probes = instrument_reader(monkeypatch)
    original_open = Session._open.__func__
    original_execute = Session._execute
    native = importlib.import_module("agents._native")
    original_native_open = native._open_conversation
    opened, open_observations, execute_calls, native_opens = [], [], [], []

    async def capture_open(cls, *args, **kwargs):
        session = await original_open(cls, *args, **kwargs)
        opened.append(session)
        open_observations.append(
            {
                "atomic": kwargs.get("_atomic_first_turn"),
                "shared": getattr(getattr(session, "_engine", None), "_runtime_is_shared", None),
                "no_home": getattr(session, "_home", None) is None,
            }
        )
        return session

    async def capture_execute(self, *args, **kwargs):
        execute_calls.append(self.id)
        return await original_execute(self, *args, **kwargs)

    def capture_native_open(*args, **kwargs):
        native_opens.append(True)
        return original_native_open(*args, **kwargs)

    monkeypatch.setattr(Session, "_open", classmethod(capture_open))
    monkeypatch.setattr(Session, "_execute", capture_execute)
    monkeypatch.setattr(native, "_open_conversation", capture_native_open)
    async with timeout(15), json_peer() as peer:
        gate = asyncio.Event()
        peer.body_gate = gate
        peer.json_replies.extend(
            json_response(f"answer-{index}", f"response-{index}", f"item-{index}")
            for index in range(3)
        )
        async with NativeProvider(
            base_url=peer.url, api_key="synthetic-concurrent-key", response_mode="buffered"
        ) as provider:
            agent = Agent(model="gpt-5.2", instructions="Reply.", provider=provider)
            tasks = [asyncio.create_task(agent.run("independent-input-0"))]
            try:
                await peer.body_entered.wait()
                peer.body_entered.clear()
                tasks.append(asyncio.create_task(agent.run("independent-input-1")))
                await peer.body_entered.wait()
                assert len(peer.requests) == 2
                assert len(probes) == 2
                assert all(not task.done() for task in tasks)

                tasks[0].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[0]
                assert not tasks[1].done()
                gate.set()
                survivor = await tasks[1]
                assert survivor.final_output == "answer-1"

                peer.body_gate = None
                recovery = await agent.run("independent-input-2")
                assert recovery.final_output == "answer-2"
                assert recovery.turn.turn_id != survivor.turn.turn_id
                assert recovery.turn.submission_id != survivor.turn.submission_id
                assert len(probes) == 3
                assert len(peer.requests) == 3
                for index, request in enumerate(peer.requests):
                    rendered = json.dumps(request["input"])
                    assert f"independent-input-{index}" in rendered
                    assert all(
                        f"independent-input-{other}" not in rendered
                        for other in range(3)
                        if other != index
                    )
            finally:
                gate.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    assert open_observations == [atomic_opening_placement()] * 3
    assert len(opened) == len(native_opens) == len(probes) == 3
    assert len({session.id for session in opened}) == 3
    assert sorted(execute_calls) == sorted(session.id for session in opened)
    assert all(session._closed and session._resources_retired for session in opened)
    assert all(
        session._reader.done()
        and not session._reader.cancelled()
        and session._reader.exception() is None
        for session in opened
    )
