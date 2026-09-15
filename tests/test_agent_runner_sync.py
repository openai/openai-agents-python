import asyncio
import threading
import time
from collections.abc import Generator
from contextlib import suppress
from typing import Any, Protocol, cast

import pytest

from agents.agent import Agent
from agents.exceptions import _mark_error_data_redacted
from agents.models import _openai_shared
from agents.models.interface import ModelProvider
from agents.models.multi_provider import MultiProvider, MultiProviderMap
from agents.run import AgentRunner
from agents.run_config import RunConfig, SandboxRunConfig
from agents.run_internal.sync import (
    _SYNC_BACKGROUND_TASKS,
    _get_pending_sync_background_tasks,
    _get_sync_loop,
    _stop_sync_loop_driver,
    _track_sync_background_task,
)
from agents.testing.model import ScriptedModel


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


def test_run_sync_uses_default_loop_for_explicit_async_dependency(
    monkeypatch, fresh_event_loop_policy
):
    runner = AgentRunner()
    observed_loops: list[asyncio.AbstractEventLoop] = []

    async def fake_run(self, *_args, session=None, **_kwargs):
        observed_loops.append(asyncio.get_running_loop())
        assert session is not None
        assert session.loop is observed_loops[-1]
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    dependency_loop = asyncio.new_event_loop()
    fresh_event_loop_policy.set_event_loop(dependency_loop)

    class _LoopBoundDependency:
        loop = dependency_loop

    try:
        runner.run_sync(Agent(name="test-agent"), "input", session=_LoopBoundDependency())
        assert observed_loops == [dependency_loop]
        assert not dependency_loop.is_running()
    finally:
        fresh_event_loop_policy.set_event_loop(None)
        dependency_loop.close()


def test_run_sync_uses_default_loop_for_explicit_model_provider(
    monkeypatch, fresh_event_loop_policy
):
    runner = AgentRunner()
    observed_loops: list[asyncio.AbstractEventLoop] = []

    class _Provider(ModelProvider):
        def get_model(self, _model_name):
            return ScriptedModel()

    async def fake_run(self, *_args, **_kwargs):
        observed_loops.append(asyncio.get_running_loop())
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    dependency_loop = asyncio.new_event_loop()
    fresh_event_loop_policy.set_event_loop(dependency_loop)

    try:
        runner.run_sync(
            Agent(name="test-agent"),
            "input",
            run_config=RunConfig(model_provider=_Provider()),
        )
        assert observed_loops == [dependency_loop]
        assert not dependency_loop.is_running()
    finally:
        fresh_event_loop_policy.set_event_loop(None)
        dependency_loop.close()


def test_run_sync_uses_default_loop_for_explicit_multi_provider(
    monkeypatch, fresh_event_loop_policy
):
    runner = AgentRunner()
    observed_loops: list[asyncio.AbstractEventLoop] = []

    dependency_loop = asyncio.new_event_loop()
    fresh_event_loop_policy.set_event_loop(dependency_loop)

    class _LoopBoundProvider(ModelProvider):
        loop = dependency_loop

        def get_model(self, _model_name):
            return ScriptedModel()

    provider_map = MultiProviderMap()
    provider_map.add_provider("custom", _LoopBoundProvider())
    provider = MultiProvider(provider_map=provider_map)

    async def fake_run(self, *_args, **_kwargs):
        observed_loops.append(asyncio.get_running_loop())
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    try:
        runner.run_sync(
            Agent(name="test-agent"),
            "input",
            run_config=RunConfig(model_provider=provider),
        )
        assert observed_loops == [dependency_loop]
        assert not dependency_loop.is_running()
    finally:
        fresh_event_loop_policy.set_event_loop(None)
        dependency_loop.close()


@pytest.mark.parametrize("run_config", [{"model": object()}, {"sandbox": {"session": object()}}])
def test_run_sync_uses_default_loop_for_dict_async_dependency(
    monkeypatch, fresh_event_loop_policy, run_config
):
    runner = AgentRunner()
    observed_loops: list[asyncio.AbstractEventLoop] = []

    async def fake_run(self, *_args, **_kwargs):
        observed_loops.append(asyncio.get_running_loop())
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    dependency_loop = asyncio.new_event_loop()
    fresh_event_loop_policy.set_event_loop(dependency_loop)

    try:
        runner.run_sync(Agent(name="test-agent"), "input", run_config=run_config)
        assert observed_loops == [dependency_loop]
        assert not dependency_loop.is_running()
    finally:
        fresh_event_loop_policy.set_event_loop(None)
        dependency_loop.close()


@pytest.mark.parametrize(
    ("agent", "kwargs"),
    [
        (Agent(name="test-agent", tools=[object()]), {}),
        (Agent(name="test-agent"), {"context": object()}),
        (Agent(name="test-agent"), {"hooks": object()}),
        (Agent(name="test-agent"), {"error_handlers": {"max_turns": lambda _data: None}}),
        (
            Agent(name="test-agent"),
            {"run_config": RunConfig(sandbox=SandboxRunConfig(client=object()))},
        ),
        (
            Agent(name="test-agent"),
            {"run_config": {"sandbox": {"client": object()}}},
        ),
    ],
)
def test_run_sync_uses_default_loop_for_caller_owned_run_surfaces(
    monkeypatch, fresh_event_loop_policy, agent, kwargs
):
    runner = AgentRunner()
    observed_loops: list[asyncio.AbstractEventLoop] = []

    async def fake_run(self, *_args, **_kwargs):
        observed_loops.append(asyncio.get_running_loop())
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    dependency_loop = asyncio.new_event_loop()
    fresh_event_loop_policy.set_event_loop(dependency_loop)
    try:
        runner.run_sync(agent, "input", **kwargs)
        assert observed_loops == [dependency_loop]
        assert not dependency_loop.is_running()
    finally:
        fresh_event_loop_policy.set_event_loop(None)
        dependency_loop.close()


def test_run_sync_force_cancels_timed_out_cleanup_on_caller_loop(
    monkeypatch, fresh_event_loop_policy
):
    runner = AgentRunner()
    cleanup_started = threading.Event()
    cleanup_cancelled = threading.Event()
    cleanup_finally_finished = threading.Event()
    nested_cancelled = threading.Event()
    nested_finally_finished = threading.Event()

    async def fake_run(self, *_args, **_kwargs):
        from agents.sandbox._cleanup_owner import create_cleanup_owner

        async def deferred_work() -> None:
            cleanup_started.set()

            async def nested_work() -> None:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    nested_cancelled.set()
                    raise
                finally:
                    await asyncio.sleep(0)
                    nested_finally_finished.set()

            nested_task = create_cleanup_owner(nested_work(), name="test.nested_cleanup")
            _track_sync_background_task(nested_task)
            try:
                await asyncio.shield(nested_task)
            except asyncio.CancelledError:
                cleanup_cancelled.set()
                raise
            finally:
                await asyncio.sleep(0)
                cleanup_finally_finished.set()

        _track_sync_background_task(create_cleanup_owner(deferred_work(), name="test.cleanup"))
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)
    monkeypatch.setattr("agents.run._SYNC_BACKGROUND_SETTLEMENT_TIMEOUT_S", 0.01)

    caller_loop = asyncio.new_event_loop()
    fresh_event_loop_policy.set_event_loop(caller_loop)
    try:
        runner.run_sync(Agent(name="test-agent"), "input", context=object())
        assert cleanup_started.is_set()
        assert cleanup_cancelled.is_set()
        assert cleanup_finally_finished.is_set()
        assert nested_cancelled.is_set()
        assert nested_finally_finished.is_set()
        assert not _get_pending_sync_background_tasks(caller_loop)
    finally:
        fresh_event_loop_policy.set_event_loop(None)
        caller_loop.close()


def test_run_sync_keeps_global_client_provider_on_caller_loop(monkeypatch, fresh_event_loop_policy):
    runner = AgentRunner()
    observed_loops: list[asyncio.AbstractEventLoop] = []
    dependency_loop = asyncio.new_event_loop()
    fresh_event_loop_policy.set_event_loop(dependency_loop)
    monkeypatch.setattr(_openai_shared, "_default_openai_client", object())

    async def fake_run(self, *_args, **_kwargs):
        observed_loops.append(asyncio.get_running_loop())
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)
    try:
        runner.run_sync(Agent(name="test-agent"), "input", run_config=RunConfig())
        assert observed_loops == [dependency_loop]
        assert not dependency_loop.is_running()
    finally:
        fresh_event_loop_policy.set_event_loop(None)
        dependency_loop.close()


def test_run_sync_settles_deferred_cleanup_before_return_on_dependency_loop(
    monkeypatch, fresh_event_loop_policy
):
    runner = AgentRunner()
    deferred_finished = threading.Event()

    dependency_loop = asyncio.new_event_loop()
    fresh_event_loop_policy.set_event_loop(dependency_loop)

    class _LoopBoundDependency:
        loop = dependency_loop

    async def fake_run(self, *_args, session=None, **_kwargs):
        assert session is not None
        assert asyncio.get_running_loop() is dependency_loop

        async def deferred_work():
            await asyncio.sleep(0.02)
            deferred_finished.set()

        task = asyncio.create_task(deferred_work())
        _track_sync_background_task(task)
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)

    try:
        runner.run_sync(
            Agent(name="test-agent"),
            "input",
            session=_LoopBoundDependency(),
        )
        assert deferred_finished.is_set()
        assert not dependency_loop.is_running()
        fresh_event_loop_policy.set_event_loop(None)
        dependency_loop.close()
    finally:
        fresh_event_loop_policy.set_event_loop(None)
        if not dependency_loop.is_closed():
            dependency_loop.close()


def test_run_sync_bounds_deferred_cleanup_on_dependency_loop(monkeypatch, fresh_event_loop_policy):
    runner = AgentRunner()
    cleanup_started = threading.Event()
    deferred_tasks: list[asyncio.Task[None]] = []

    async def fake_run(self, *_args, **_kwargs):
        async def deferred_work() -> None:
            cleanup_started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(deferred_work())
        deferred_tasks.append(task)
        _track_sync_background_task(task)
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)
    monkeypatch.setattr("agents.run._SYNC_BACKGROUND_SETTLEMENT_TIMEOUT_S", 0.01)

    dependency_loop = asyncio.new_event_loop()
    fresh_event_loop_policy.set_event_loop(dependency_loop)
    try:
        started_at = time.monotonic()
        runner.run_sync(Agent(name="test-agent", tools=[object()]), "input")
        elapsed = time.monotonic() - started_at

        assert cleanup_started.is_set()
        assert elapsed < 0.5
        assert deferred_tasks and deferred_tasks[0].cancelled()
        assert not dependency_loop.is_running()
    finally:
        for task in deferred_tasks:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                dependency_loop.run_until_complete(task)
        fresh_event_loop_policy.set_event_loop(None)
        dependency_loop.close()


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


def test_run_sync_uses_sdk_loop_when_caller_owned_surface_has_no_default_loop(
    monkeypatch, fresh_event_loop_policy
):
    runner = AgentRunner()
    cleanup_started = threading.Event()
    cleanup_finished = threading.Event()
    release_cleanup = threading.Event()
    deferred_tasks: list[asyncio.Task[None]] = []

    async def fake_run(self, *_args, **_kwargs):
        async def deferred_work():
            cleanup_started.set()
            await asyncio.to_thread(release_cleanup.wait)
            cleanup_finished.set()

        task = asyncio.create_task(deferred_work())
        deferred_tasks.append(task)
        _track_sync_background_task(task)
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)
    monkeypatch.setattr("agents.run._SYNC_BACKGROUND_SETTLEMENT_TIMEOUT_S", 0.01)
    fresh_event_loop_policy.set_event_loop(None)

    try:
        started_at = time.monotonic()
        runner.run_sync(Agent(name="test-agent"), "input", context=object())
        elapsed = time.monotonic() - started_at
        assert cleanup_started.is_set()
        assert elapsed < 0.5
        assert deferred_tasks and not deferred_tasks[0].done()
        assert not cleanup_finished.is_set()

        release_cleanup.set()
        assert cleanup_finished.wait(timeout=0.5)
    finally:
        release_cleanup.set()
        sync_loop = _get_sync_loop()
        _stop_sync_loop_driver(sync_loop)
        if not sync_loop.is_closed():
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


def test_run_sync_preserves_sandbox_resume_state_when_redacting_cancellation(monkeypatch):
    runner = AgentRunner()
    resume_state = {"backend_id": "preserved"}

    def fake_run_sync_impl(*_args, **_kwargs):
        error = asyncio.CancelledError("sensitive cancellation")
        cast(Any, error)._sandbox_resume_state = resume_state
        _mark_error_data_redacted(error)
        raise error

    monkeypatch.setattr(runner, "_run_sync_impl", fake_run_sync_impl)

    with pytest.raises(asyncio.CancelledError) as exc_info:
        runner.run_sync(Agent(name="test-agent"), "input")

    assert getattr(exc_info.value, "_sandbox_resume_state", None) == resume_state


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


def test_run_sync_does_not_cancel_async_generator_finalization_on_handoff(
    monkeypatch, fresh_event_loop_policy
):
    runner = AgentRunner()
    shutdown_started = threading.Event()
    release_shutdown = threading.Event()
    generator_finished = threading.Event()
    held_generators: list[object] = []
    run_count = 0

    class _ControlledLoop(asyncio.SelectorEventLoop):
        async def shutdown_asyncgens(self):
            shutdown_started.set()
            await asyncio.to_thread(release_shutdown.wait)
            await super().shutdown_asyncgens()

    async def fake_run(self, *_args, **_kwargs):
        nonlocal run_count
        run_count += 1
        if run_count == 1:

            async def agen():
                try:
                    yield None
                finally:
                    generator_finished.set()

            gen = agen()
            await gen.__anext__()
            held_generators.append(gen)

            async def deferred_work():
                await asyncio.sleep(0)

            _track_sync_background_task(asyncio.create_task(deferred_work()))
        return object()

    monkeypatch.setattr(AgentRunner, "run", fake_run, raising=False)
    sync_loop = _ControlledLoop()
    monkeypatch.setattr("agents.run._get_sync_loop", lambda: sync_loop)
    fresh_event_loop_policy.set_event_loop(None)

    release_thread = threading.Thread(
        target=lambda: (shutdown_started.wait(), release_shutdown.set()), daemon=True
    )
    try:
        runner.run_sync(Agent(name="test-agent"), "input")
        assert shutdown_started.wait(timeout=0.5)
        release_thread.start()
        runner.run_sync(Agent(name="test-agent"), "input")
        assert generator_finished.is_set()
    finally:
        release_shutdown.set()
        if release_thread.is_alive():
            release_thread.join(timeout=0.5)
        monkeypatch.undo()
        _stop_sync_loop_driver(sync_loop)
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
