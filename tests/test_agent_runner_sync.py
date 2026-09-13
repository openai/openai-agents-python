import asyncio
import threading
from collections.abc import Generator
from typing import Protocol

import pytest

from agents.agent import Agent
from agents.run import AgentRunner
from agents.run_internal.sync import (
    _SYNC_BACKGROUND_TASKS,
    _get_pending_sync_background_tasks,
    _get_sync_loop,
    _stop_sync_loop_driver,
    _track_sync_background_task,
)


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


def test_run_sync_does_not_drive_existing_default_loop(monkeypatch, fresh_event_loop_policy):
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
        assert observed_loops and observed_loops[0] is not test_loop
        assert not test_loop.is_running()
    finally:
        fresh_event_loop_policy.set_event_loop(None)
        test_loop.close()


def test_run_sync_leaves_caller_loop_closable_with_deferred_cleanup(
    monkeypatch, fresh_event_loop_policy
):
    runner = AgentRunner()
    deferred_finished = threading.Event()

    async def fake_run(self, *_args, **_kwargs):
        async def deferred_work():
            await asyncio.sleep(0.02)
            deferred_finished.set()

        task = asyncio.create_task(deferred_work())
        _track_sync_background_task(task)
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    caller_loop = asyncio.new_event_loop()
    fresh_event_loop_policy.set_event_loop(caller_loop)
    try:
        runner.run_sync(Agent(name="test-agent"), "input")
        fresh_event_loop_policy.set_event_loop(None)
        caller_loop.close()

        assert not caller_loop.is_running()
        assert deferred_finished.wait(timeout=0.5)
    finally:
        _stop_sync_loop_driver(_get_sync_loop())
        fresh_event_loop_policy.set_event_loop(None)
        if not caller_loop.is_closed():
            caller_loop.close()


def test_run_sync_does_not_create_or_replace_default_loop_when_missing(
    monkeypatch, fresh_event_loop_policy
):
    runner = AgentRunner()
    observed_loops: list[asyncio.AbstractEventLoop] = []

    async def fake_run(self, *_args, **_kwargs):
        observed_loops.append(asyncio.get_running_loop())
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    fresh_event_loop_policy.set_event_loop(None)

    runner.run_sync(Agent(name="test-agent"), "input")
    assert observed_loops
    with pytest.raises(RuntimeError):
        fresh_event_loop_policy.get_event_loop()

    sync_loop = _get_sync_loop()
    _stop_sync_loop_driver(sync_loop)
    sync_loop.close()


def test_run_sync_does_not_replace_closed_default_loop(monkeypatch, fresh_event_loop_policy):
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
        assert observed_loops
        assert fresh_event_loop_policy.get_event_loop() is closed_loop
        assert closed_loop.is_closed()
    finally:
        fresh_event_loop_policy.set_event_loop(None)
        sync_loop = _get_sync_loop()
        _stop_sync_loop_driver(sync_loop)
        if not sync_loop.is_closed():
            sync_loop.close()


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
    started = threading.Event()
    cancelled = threading.Event()

    async def fake_run(self, *_args, **_kwargs):
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    sync_loop = asyncio.new_event_loop()
    original_run_until_complete = sync_loop.run_until_complete
    call_count = 0

    def interrupt_once(future):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            original_run_until_complete(asyncio.sleep(0))
            raise KeyboardInterrupt()
        return original_run_until_complete(future)

    monkeypatch.setattr("agents.run._get_sync_loop", lambda: sync_loop)
    monkeypatch.setattr(sync_loop, "run_until_complete", interrupt_once)

    try:
        with pytest.raises(KeyboardInterrupt):
            runner.run_sync(Agent(name="test-agent"), "input")

        assert started.is_set(), "Expected run_sync to schedule a task."
        assert cancelled.is_set(), "Expected run_sync to cancel the task after interruption."
        assert call_count >= 2
    finally:
        monkeypatch.undo()
        _stop_sync_loop_driver(sync_loop)
        sync_loop.close()


def test_sync_background_registry_prunes_completed_tasks():
    sync_loop = asyncio.new_event_loop()
    try:
        task = sync_loop.create_task(asyncio.sleep(0))
        sync_loop.run_until_complete(task)
        _SYNC_BACKGROUND_TASKS[sync_loop] = {task}

        assert _get_pending_sync_background_tasks(sync_loop) == ()
        assert sync_loop not in _SYNC_BACKGROUND_TASKS
    finally:
        sync_loop.close()


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


def test_run_sync_shutdowns_asyncgens_after_tracked_cleanup(monkeypatch, fresh_event_loop_policy):
    runner = AgentRunner()
    cleanup_finished = threading.Event()
    generator_finished = threading.Event()
    order: list[str] = []
    held_generators: list[object] = []

    async def fake_run(self, *_args, **_kwargs):
        async def agen():
            try:
                yield None
            finally:
                order.append("generator")
                generator_finished.set()

        gen = agen()
        await gen.__anext__()
        held_generators.append(gen)

        async def deferred_work():
            order.append("cleanup-start")
            await asyncio.sleep(0.02)
            order.append("cleanup-finished")
            cleanup_finished.set()

        task = asyncio.create_task(deferred_work())
        _track_sync_background_task(task)
        return "ok"

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    try:
        runner.run_sync(Agent(name="test-agent"), "input")
        assert cleanup_finished.wait(timeout=0.5)
        assert generator_finished.wait(timeout=0.5)
        assert order == ["cleanup-start", "cleanup-finished", "generator"]
    finally:
        sync_loop = _get_sync_loop()
        _stop_sync_loop_driver(sync_loop)
        if not sync_loop.is_closed():
            sync_loop.close()


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
        _stop_sync_loop_driver(_get_sync_loop())
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
        _stop_sync_loop_driver(_get_sync_loop())
        release.set()
        runner.run_sync(Agent(name="test-agent"), "input")
        assert background_finished.is_set()
    finally:
        _stop_sync_loop_driver(_get_sync_loop())
        fresh_event_loop_policy.set_event_loop(None)
        test_loop.close()
