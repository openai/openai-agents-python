"""Native run recovery owns queued and entered application callbacks."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agents import Agent, PromptedOutput, RecoverRun
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_agent_observation import provider
from tests.test_native_one_shot_error_handler import Record


@pytest.mark.parametrize("entered", [False, True])
async def test_cancelled_recovery_revokes_queued_handler_and_joins_entered_handler(
    responses, monkeypatch, entered
):
    responses.respond(assistant_output("bad output"))
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    release_worker = threading.Event()
    queued = asyncio.Event()
    entered_handler = asyncio.Event()
    calls = []

    def hold_worker():
        assert release_worker.wait(10), "Test did not release the worker"

    if not entered:
        busy = loop.run_in_executor(None, hold_worker)
    original = loop.run_in_executor

    def observe_worker(executor, callback, *args):
        worker = original(executor, callback, *args)
        queued.set()
        return worker

    def recover(failure):
        calls.append(failure.kind)
        loop.call_soon_threadsafe(entered_handler.set)
        if entered:
            hold_worker()
        return RecoverRun(Record(count=3))

    native = provider(responses)
    agent = Agent(
        instructions="Return a record.",
        provider=native,
        output_type=PromptedOutput(Record, max_retries=0),
    )
    monkeypatch.setattr(loop, "run_in_executor", observe_worker)
    async with timeout(15):
        running = asyncio.create_task(agent.run("begin", error_handler=recover))
        try:
            await (entered_handler.wait() if entered else queued.wait())
            running.cancel()
            await asyncio.sleep(0)
            assert not running.done()
        finally:
            release_worker.set()
        try:
            with pytest.raises(asyncio.CancelledError):
                await running
            assert calls == (["invalid_output"] if entered else [])
            assert len(responses.requests) == 1
        finally:
            if not entered:
                await busy
            await native.aclose()
