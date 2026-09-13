import asyncio
import threading
from collections.abc import Generator
from typing import Any, Protocol

import pytest

from agents.agent import Agent
from agents.run import AgentRunner
from agents.run_internal.sync import _stop_sync_loop_driver, _track_sync_background_task


class _EventLoopPolicy(Protocol):
    def get_event_loop(self) -> asyncio.AbstractEventLoop: ...

    def set_event_loop(self, loop: asyncio.AbstractEventLoop | None) -> None: ...


@pytest.fixture
def fresh_event_loop_policy() -> Generator[_EventLoopPolicy, None, None]:
    policy_before = asyncio.get_event_loop_policy()
    new_policy = type(policy_before)()
    asyncio.set_event_loop_policy(new_policy)
    try:
        yield new_policy
    finally:
        asyncio.set_event_loop_policy(policy_before)


def test_run_sync_reuses_existing_default_loop(monkeypatch, fresh_event_loop_policy):
    runner = AgentRunner()
    observed_loops: list[asyncio.AbstractEventLoop] = []

    async def fake_run(self, *_args, **_kwargs):
        observed_loops.append(asyncio.get_running_loop())
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    test_loop = asyncio.new_event_loop()
    fresh_event_loop_policy.set_event_loop(test_loop)

    try:
        runner.run_sync(Agent(name="test-agent"), "input")
        assert observed_loops and observed_loops[0] is test_loop
    finally:
        fresh_event_loop_policy.set_event_loop(None)
        test_loop.close()


def test_run_sync_creates_default_loop_when_missing(monkeypatch, fresh_event_loop_policy):
    runner = AgentRunner()
    observed_loops: list[asyncio.AbstractEventLoop] = []

    async def fake_run(self, *_args, **_kwargs):
        observed_loops.append(asyncio.get_running_loop())
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    fresh_event_loop_policy.set_event_loop(None)

    runner.run_sync(Agent(name="test-agent"), "input")
    created_loop = observed_loops[0]
    assert created_loop is fresh_event_loop_policy.get_event_loop()

    fresh_event_loop_policy.set_event_loop(None)
    created_loop.close()


def test_run_sync_replaces_closed_default_loop(monkeypatch, fresh_event_loop_policy):
    runner = AgentRunner()
    observed_loops: list[asyncio.AbstractEventLoop] = []

    async def fake_run(self, *_args, **_kwargs):
        observed_loops.append(asyncio.get_running_loop())
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    closed_loop = asyncio.new_event_loop()
    fresh_event_loop_policy.set_event_loop(closed_loop)
    closed_loop.close()

    try:
        runner.run_sync(Agent(name="test-agent"), "input")
        replacement_loop = observed_loops[0]
        assert replacement_loop is fresh_event_loop_policy.get_event_loop()
        assert replacement_loop is not closed_loop
        assert not replacement_loop.is_closed()
    finally:
        current_loop = fresh_event_loop_policy.get_event_loop()
        fresh_event_loop_policy.set_event_loop(None)
        if not current_loop.is_closed():
            current_loop.close()


def test_run_sync_errors_when_loop_already_running(monkeypatch, fresh_event_loop_policy):
    runner = AgentRunner()

    async def fake_run(self, *_args, **_kwargs):
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    async def invoke():
        with pytest.raises(RuntimeError):
            runner.run_sync(Agent(name="test-agent"), "input")

    asyncio.run(invoke())


def test_run_sync_cancels_task_when_interrupted(monkeypatch, fresh_event_loop_policy):
    runner = AgentRunner()

    async def fake_run(self, *_args, **_kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    test_loop = asyncio.new_event_loop()
    fresh_event_loop_policy.set_event_loop(test_loop)

    created_tasks: list[asyncio.Task[Any]] = []
    original_create_task = test_loop.create_task

    def capturing_create_task(coro):
        task = original_create_task(coro)
        created_tasks.append(task)
        return task

    original_run_until_complete = test_loop.run_until_complete
    call_count = {"value": 0}

    def interrupt_once(future):
        call_count["value"] += 1
        if call_count["value"] == 1:
            raise KeyboardInterrupt()
        return original_run_until_complete(future)

    monkeypatch.setattr(test_loop, "create_task", capturing_create_task)
    monkeypatch.setattr(test_loop, "run_until_complete", interrupt_once)

    try:
        with pytest.raises(KeyboardInterrupt):
            runner.run_sync(Agent(name="test-agent"), "input")

        assert created_tasks, "Expected run_sync to schedule a task."
        assert created_tasks[0].done()
        assert created_tasks[0].cancelled()
        assert call_count["value"] >= 2
    finally:
        monkeypatch.undo()
        fresh_event_loop_policy.set_event_loop(None)
        test_loop.close()


def test_run_sync_finalizes_async_generators(monkeypatch, fresh_event_loop_policy):
    runner = AgentRunner()
    cleanup_markers: list[str] = []

    async def fake_run(self, *_args, **_kwargs):
        async def agen():
            try:
                yield None
            finally:
                cleanup_markers.append("done")

        gen = agen()
        await gen.__anext__()
        return "ok"

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    test_loop = asyncio.new_event_loop()
    fresh_event_loop_policy.set_event_loop(test_loop)

    try:
        runner.run_sync(Agent(name="test-agent"), "input")
        assert cleanup_markers == ["done"], (
            "Async generators must be finalized after run_sync returns."
        )
    finally:
        fresh_event_loop_policy.set_event_loop(None)
        test_loop.close()


def test_run_sync_drives_tracked_background_task_after_return(monkeypatch, fresh_event_loop_policy):
    runner = AgentRunner()
    completed = threading.Event()

    async def fake_run(self, *_args, **_kwargs):
        async def deferred_work():
            await asyncio.sleep(0.01)
            completed.set()

        task = asyncio.create_task(deferred_work())
        _track_sync_background_task(task)
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    test_loop = asyncio.new_event_loop()
    fresh_event_loop_policy.set_event_loop(test_loop)

    try:
        runner.run_sync(Agent(name="test-agent"), "input")
        assert completed.wait(timeout=0.5)
    finally:
        _stop_sync_loop_driver(test_loop)
        fresh_event_loop_policy.set_event_loop(None)
        test_loop.close()


def test_run_sync_does_not_stop_next_run_when_old_task_finishes(
    monkeypatch, fresh_event_loop_policy
):
    runner = AgentRunner()
    release = threading.Event()
    background_finished = threading.Event()
    run_count = 0

    async def fake_run(self, *_args, **_kwargs):
        nonlocal run_count
        run_count += 1
        if run_count == 1:

            async def deferred_work():
                await asyncio.to_thread(release.wait)
                background_finished.set()

            task = asyncio.create_task(deferred_work())
            _track_sync_background_task(task)
        else:
            await asyncio.sleep(0.05)
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    test_loop = asyncio.new_event_loop()
    fresh_event_loop_policy.set_event_loop(test_loop)

    try:
        runner.run_sync(Agent(name="test-agent"), "input")
        _stop_sync_loop_driver(test_loop)
        release.set()
        runner.run_sync(Agent(name="test-agent"), "input")
        assert background_finished.is_set()
    finally:
        _stop_sync_loop_driver(test_loop)
        fresh_event_loop_policy.set_event_loop(None)
        test_loop.close()
