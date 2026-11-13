"""Recovery keeps ownership of its replacement output guardrails on cancellation."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agents import Agent, GuardrailResult, OutputGuardrail, PromptedOutput, RecoverRun
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_agent_observation import provider
from tests.test_native_one_shot_error_handler import Record


@pytest.mark.parametrize("entered", [False, True])
async def test_recovery_cancellation_revokes_replacement_guardrail_and_joins_entered_guard(
    responses, monkeypatch, entered
):
    responses.respond(assistant_output("bad output"))
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    release_worker = threading.Event()
    queued = asyncio.Event()
    entered_guard = asyncio.Event()
    recovered = False
    calls = []

    def hold_worker():
        assert release_worker.wait(10), "Test did not release the worker"

    busy = None
    original = loop.run_in_executor

    def observe_worker(executor, callback, *args):
        worker = original(executor, callback, *args)
        if recovered:
            queued.set()
        return worker

    async def recover(failure):
        nonlocal busy, recovered
        assert failure.kind == "invalid_output"
        if not entered:
            busy = original(None, hold_worker)
        recovered = True
        return RecoverRun(Record(count=3))

    def guard(context, agent):
        calls.append(context.final_output)
        loop.call_soon_threadsafe(entered_guard.set)
        if entered:
            hold_worker()
        return GuardrailResult(False)

    native = provider(responses)
    agent = Agent(
        instructions="Return a record.",
        provider=native,
        output_type=PromptedOutput(Record, max_retries=0),
        output_guardrails=[OutputGuardrail("replacement", guard)],
    )
    monkeypatch.setattr(loop, "run_in_executor", observe_worker)
    async with timeout(15):
        running = asyncio.create_task(agent.run("begin", error_handler=recover))
        try:
            await (entered_guard.wait() if entered else queued.wait())
            running.cancel()
            await asyncio.sleep(0)
            assert not running.done()
        finally:
            release_worker.set()
        try:
            with pytest.raises(asyncio.CancelledError):
                await running
            assert calls == ([Record(count=3)] if entered else [])
            assert len(responses.requests) == 1
        finally:
            if busy is not None:
                await busy
            await native.aclose()
