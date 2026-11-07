"""Public migration errors retain the complete native cleanup owner."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from agents import NativeProvider, Session, get_cleanup as native_get_cleanup
from agents.classic import Agent
from agents.migration import SDKRunner, _runner, get_cleanup

from .test_migration_runner import ScriptedModel, message


@pytest.fixture(autouse=True)
def loopback_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")


@pytest.mark.parametrize("entry", ["run", "start"])
@pytest.mark.parametrize("stage", ["session", "provider"])
@pytest.mark.parametrize("model_failure", [False, True])
async def test_run_error_exposes_complete_cleanup(
    monkeypatch: pytest.MonkeyPatch, entry: str, stage: str, model_failure: bool
) -> None:
    cleanup_error = OSError("transient migration retirement")
    primary = RuntimeError("original model error")
    failed = False
    model_calls = 0
    providers: list[NativeProvider] = []
    carriers: list[_runner._ResponseCarrier] = []
    original_session_close = Session._close_session
    original_provider_close = NativeProvider.aclose
    original_carrier_close = _runner._ResponseCarrier.close

    async def session_close(self: Any) -> None:
        nonlocal failed
        if stage == "session" and not failed:
            failed = True
            raise cleanup_error
        await original_session_close(self)

    async def provider_close(self: NativeProvider) -> None:
        nonlocal failed
        if self not in providers:
            providers.append(self)
        if stage == "provider" and not failed:
            failed = True
            raise cleanup_error
        await original_provider_close(self)

    async def carrier_close(self: _runner._ResponseCarrier) -> None:
        carriers.append(self)
        await original_carrier_close(self)

    class SelectedModel(ScriptedModel):
        async def get_response(self, *args: Any, **kwargs: Any):
            nonlocal model_calls
            model_calls += 1
            if model_failure:
                raise primary
            return await super().get_response(*args, **kwargs)

    monkeypatch.setattr(Session, "_close_session", session_close)
    monkeypatch.setattr(NativeProvider, "aclose", provider_close)
    monkeypatch.setattr(_runner._ResponseCarrier, "close", carrier_close)
    model = SelectedModel([message("done")])
    agent = Agent("Cleanup", model=model)
    operation = (
        SDKRunner.run(agent, "hello")
        if entry == "run"
        else SDKRunner.start(agent, "hello").get_result()
    )
    with pytest.raises(BaseException) as raised:
        await operation
    error = raised.value
    assert error is (primary if model_failure else cleanup_error)
    if model_failure:
        assert error.__context__ is cleanup_error
    cleanup = get_cleanup(error)
    assert cleanup is getattr(error, "cleanup", None)
    assert isinstance(cleanup, Session)
    assert getattr(error, "provider", None) is cleanup._owned_provider
    assert carriers and all(not carrier.active for carrier in carriers)
    assert all(not carrier._tasks and not carrier._writers for carrier in carriers)
    assert all(
        carrier._server is not None and not carrier._server.is_serving() for carrier in carriers
    )
    await cleanup.aclose()
    await cleanup.aclose()
    assert len(providers) == 1 and vars(providers[0]).get("_closed") is True
    assert model_calls == 1
    assert model.outputs == ([[message("done")]] if model_failure else [])


@pytest.mark.parametrize("consumer", ["run", "cancel_handle", "cancel_waiter", "cancel_replay"])
@pytest.mark.parametrize("stage", ["session", "provider"])
async def test_cancelled_run_retains_public_cleanup(
    monkeypatch: pytest.MonkeyPatch, consumer: str, stage: str
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    failure = OSError("transient cancelled retirement")
    original_close = Session._close_session
    original_provider_close = NativeProvider.aclose
    failed = False

    async def close(self: Any) -> None:
        nonlocal failed
        if stage == "session" and not failed:
            failed = True
            raise failure
        await original_close(self)

    async def provider_close(self: NativeProvider) -> None:
        nonlocal failed
        if stage == "provider" and not failed:
            failed = True
            raise failure
        await original_provider_close(self)

    class HeldModel(ScriptedModel):
        async def get_response(self, *args: Any, **kwargs: Any):
            entered.set()
            await release.wait()
            return await super().get_response(*args, **kwargs)

    monkeypatch.setattr(Session, "_close_session", close)
    monkeypatch.setattr(NativeProvider, "aclose", provider_close)
    agent = Agent("Cancelled cleanup", model=HeldModel([message("unused")]))
    if consumer == "run":
        task = asyncio.create_task(SDKRunner.run(agent, "hello"))
    else:
        handle = SDKRunner.start(agent, "hello")
        task = asyncio.create_task(
            anext(handle.replay_events()) if consumer == "cancel_replay" else handle.get_result()
        )
    try:
        await asyncio.wait_for(entered.wait(), 15)
        if consumer == "cancel_handle":
            handle.cancel()
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        cleanup = get_cleanup(raised.value)
        assert isinstance(cleanup, Session)
        provider = cleanup._owned_provider
        assert isinstance(provider, NativeProvider)
        try:
            if consumer != "run":
                for _ in range(3):
                    with pytest.raises(asyncio.CancelledError) as repeated:
                        await handle.get_result()
                    assert get_cleanup(repeated.value) is cleanup
                with pytest.raises(asyncio.CancelledError) as repeated:
                    _ = handle.final_output
                assert get_cleanup(repeated.value) is cleanup
                with pytest.raises(asyncio.CancelledError) as repeated:
                    await anext(handle.replay_events())
                assert get_cleanup(repeated.value) is cleanup
        finally:
            await cleanup.aclose()
            await cleanup.aclose()
        assert task.cancelled()
        assert vars(provider).get("_closed") is True
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("entry", ["run", "start"])
async def test_failed_opening_retains_migration_owned_provider(
    monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    primary = RuntimeError("opening failed")
    cleanup_error = OSError("transient provider retirement")
    failed = False
    original_close = NativeProvider.aclose

    async def open_session(*args: Any, **kwargs: Any) -> None:
        raise primary

    async def close(self: NativeProvider) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise cleanup_error
        await original_close(self)

    monkeypatch.setattr(Session, "_open", open_session)
    monkeypatch.setattr(NativeProvider, "aclose", close)
    agent = Agent("Opening", model=ScriptedModel([message("unused")]))
    operation = (
        SDKRunner.run(agent, "hello")
        if entry == "run"
        else SDKRunner.start(agent, "hello").get_result()
    )
    with pytest.raises(RuntimeError) as raised:
        await operation
    assert raised.value is primary
    assert primary.__context__ is cleanup_error
    cleanup = get_cleanup(primary)
    assert cleanup is getattr(primary, "cleanup", None)
    assert isinstance(cleanup, NativeProvider)
    assert getattr(primary, "provider", None) is cleanup
    await cleanup.aclose()
    assert vars(cleanup).get("_closed") is True


@pytest.mark.parametrize("entry", ["run", "start"])
async def test_cancellation_during_terminal_close_joins_and_exposes_retry(
    monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    closing = asyncio.Event()
    release = asyncio.Event()
    failure = OSError("transient terminal retirement")
    failed = False
    original_close = Session._close_session

    async def close(self: Any) -> None:
        nonlocal failed
        if not failed:
            failed = True
            closing.set()
            await release.wait()
            raise failure
        await original_close(self)

    monkeypatch.setattr(Session, "_close_session", close)
    agent = Agent("Terminal cleanup", model=ScriptedModel([message("done")]))
    operation = (
        SDKRunner.run(agent, "hello")
        if entry == "run"
        else SDKRunner.start(agent, "hello").get_result()
    )
    task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(closing.wait(), 15)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        cleanup = get_cleanup(raised.value)
        assert isinstance(cleanup, Session)
        provider = cleanup._owned_provider
        await cleanup.aclose()
        assert vars(provider).get("_closed") is True
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_get_cleanup_retains_nearest_owner_and_does_not_change_error_chain() -> None:
    nearest = NativeProvider()
    older = NativeProvider()
    original = OSError("cleanup failed")
    cast(Any, original).cleanup = older
    cancelled = asyncio.CancelledError()
    cast(Any, cancelled).cleanup = nearest
    cancelled.__context__ = original
    replacement = asyncio.CancelledError()
    replacement.__context__ = cancelled
    cause = RuntimeError("explicit cause")
    replacement.__cause__ = cause

    assert get_cleanup(replacement) is nearest
    assert replacement.__context__ is cancelled
    assert cancelled.__context__ is original
    assert replacement.__cause__ is cause
    assert not hasattr(replacement, "cleanup")


def test_get_cleanup_ignores_provider_only_and_handles_context_cycles() -> None:
    error = RuntimeError("unrelated failure")
    cast(Any, error).cleanup = object()
    cast(Any, error).provider = NativeProvider()
    context = RuntimeError("cyclic context")
    context.__context__ = error
    error.__context__ = context

    assert get_cleanup(error) is None
    assert error.__context__ is context
    assert context.__context__ is error


def test_migration_cleanup_accessor_is_native_accessor() -> None:
    assert get_cleanup is native_get_cleanup
