"""run_sync must return even when its native orphan still owns a manager."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager

import pytest

from agents import Agent, Environment, HttpMCP, NativeProvider, function_tool, get_cleanup
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_agent import provider
from tests.test_native_environment_protocol import delayed_native_join as delayed_native_join


async def test_failed_sync_opening_retains_manager_until_explicit_borrowed_provider_close(
    responses: ResponsesFixture, delayed_native_join: list[str]
) -> None:
    shared = provider(responses)
    agent = Agent(instructions="Inspect.", model="gpt-5.5", provider=shared)
    entered, exited = [], []

    @asynccontextmanager
    async def manager():
        entered.append(asyncio.current_task())
        try:
            yield Environment(url="wss://127.0.0.1:1/exec", cwd="/workspace", connect_timeout=0.15)
        finally:
            assert len(delayed_native_join) >= 3
            exited.append(asyncio.current_task())

    async with timeout(20):
        try:
            with pytest.raises(RuntimeError) as original:
                await asyncio.to_thread(agent.run_sync, "Inspect", environment=manager())
            assert "controlled native retirement" not in str(original.value)
            assert delayed_native_join == ["join", "join"]
            assert len(entered) == 1 and not exited
            assert entered[0] is not None
            owner_loop = entered[0].get_loop()
            assert not owner_loop.is_running() and not owner_loop.is_closed()

            responses.respond(assistant_output("sibling still works"))
            async with agent.session() as sibling:
                result = await (await sibling.submit("Answer")).get_result()
                assert result.final_output == "sibling still works"
                with pytest.raises(RuntimeError, match="open or opening"):
                    await shared.aclose()
                assert not exited and delayed_native_join == ["join", "join"]
            await shared.aclose()
            assert entered == exited and delayed_native_join == ["join", "join", "join"]
            assert owner_loop.is_closed()
            await shared.aclose()
            assert entered == exited
        finally:
            await shared.aclose()


@pytest.mark.parametrize(
    ("delayed_native_join", "requires_retry"),
    [(2, False), (3, True)],
    indirect=["delayed_native_join"],
    ids=["automatic-retires", "explicit-retry"],
)
async def test_implicit_sync_opening_retains_only_an_unretired_recovery_owner(
    monkeypatch: pytest.MonkeyPatch,
    delayed_native_join: list[str],
    requires_retry: bool,
) -> None:
    from agents import _native

    # Opening fails on a real executor connection, before any model request.
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-sync-environment-test")

    async def forbid_submission(*args, **kwargs):
        pytest.fail("failed Environment startup must not submit a model request")

    monkeypatch.setattr(_native.NativeThread, "_submit", forbid_submission)
    agent = Agent(instructions="Inspect.", model="gpt-5.5")
    entered, exited = [], []

    @asynccontextmanager
    async def manager():
        entered.append(asyncio.current_task())
        try:
            yield Environment(url="wss://127.0.0.1:1/exec", cwd="/workspace", connect_timeout=0.15)
        finally:
            exited.append(asyncio.current_task())

    async with timeout(20):
        with pytest.raises(RuntimeError) as original:
            await asyncio.to_thread(agent.run_sync, "Inspect", environment=manager())
        assert "controlled native retirement" not in str(original.value)
        assert agent.provider is None
        assert delayed_native_join == ["join", "join", "join"]
        assert len(entered) == 1 and entered[0] is not None
        loop = entered[0].get_loop()
        recovery = getattr(original.value, "provider", None)
        assert get_cleanup(original.value) is recovery
        if requires_retry:
            assert not exited
            assert isinstance(recovery, NativeProvider)
            assert not loop.is_running() and not loop.is_closed()
            await recovery.aclose()
            assert delayed_native_join == ["join", "join", "join", "join"]
            await recovery.aclose()
        else:
            assert entered == exited and recovery is None and loop.is_closed()
        assert entered == exited and loop.is_closed()


@pytest.mark.parametrize(
    ("delayed_native_join", "requires_retry"),
    [(2, False), (3, True)],
    indirect=["delayed_native_join"],
    ids=["automatic-retires", "explicit-retry"],
)
async def test_implicit_async_opening_retains_only_an_unretired_recovery_owner(
    monkeypatch: pytest.MonkeyPatch,
    delayed_native_join: list[str],
    requires_retry: bool,
) -> None:
    from agents import _native

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-async-environment-test")
    retained = []
    real_defer = NativeProvider._defer_opening

    def defer(provider, opening, environment):
        retained.append(provider)
        real_defer(provider, opening, environment)

    async def forbid_submission(*args, **kwargs):
        pytest.fail("failed Environment startup must not submit a model request")

    monkeypatch.setattr(NativeProvider, "_defer_opening", defer)
    monkeypatch.setattr(_native.NativeThread, "_submit", forbid_submission)
    agent = Agent(instructions="Inspect.", model="gpt-5.5")
    entered, exited = [], []

    @asynccontextmanager
    async def manager():
        entered.append(asyncio.current_task())
        try:
            yield Environment(url="wss://127.0.0.1:1/exec", cwd="/workspace", connect_timeout=0.15)
        finally:
            exited.append(asyncio.current_task())

    async with timeout(20):
        try:
            with pytest.raises(RuntimeError) as original:
                await agent.run("Inspect", environment=manager())
            assert "controlled native retirement" not in str(original.value)
            assert agent.provider is None
            assert delayed_native_join == ["join", "join", "join"]
            assert len(entered) == 1 and entered[0].get_loop() is asyncio.get_running_loop()
            recovery = getattr(original.value, "provider", None)
            assert get_cleanup(original.value) is recovery
            if requires_retry:
                assert not exited
                assert isinstance(recovery, NativeProvider)
                await recovery.aclose()
                assert delayed_native_join == ["join", "join", "join", "join"]
                await recovery.aclose()
            else:
                assert recovery is None
            assert entered == exited
            assert not entered[0].get_loop().is_closed()
        finally:
            # Never pass via this fallback: if recovery is missing, the assertion
            # above fails. Join the real retained native lease for test teardown.
            for provider in retained:
                await provider.aclose()


async def test_sync_manager_recovery_cancellation_joins_exactly_one_exit(
    responses: ResponsesFixture, delayed_native_join: list[str]
) -> None:
    shared = provider(responses)
    agent = Agent(instructions="Inspect.", model="gpt-5.5", provider=shared)
    entered, exited = [], []
    releasing = threading.Event()
    release = threading.Event()

    @asynccontextmanager
    async def manager():
        entered.append(asyncio.current_task())
        try:
            yield Environment(url="wss://127.0.0.1:1/exec", cwd="/workspace", connect_timeout=0.15)
        finally:
            assert delayed_native_join == ["join", "join", "join"]
            releasing.set()
            await asyncio.to_thread(release.wait)
            exited.append(asyncio.current_task())

    async with timeout(20):
        try:
            with pytest.raises(RuntimeError):
                await asyncio.to_thread(agent.run_sync, "Inspect", environment=manager())
            assert len(entered) == 1 and not exited
            close = asyncio.create_task(shared.aclose())
            assert await asyncio.to_thread(releasing.wait, 8)
            assert not close.done() and not exited
            close.cancel()
            await asyncio.sleep(0)
            close.cancel()
            await asyncio.sleep(0)
            assert not close.done() and not exited
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await close
            assert entered == exited and entered[0] is not None
            assert entered[0].get_loop().is_closed()
            await shared.aclose()
            assert entered == exited
        finally:
            release.set()
            await shared.aclose()


async def test_sync_child_owned_provider_retains_only_child_manager(
    responses: ResponsesFixture, delayed_native_join: list[str]
) -> None:
    outer_provider = provider(responses)
    inner_provider = NativeProvider(base_url="http://127.0.0.1:1/v1", api_key="fixture")
    entered, exited = [], []

    @asynccontextmanager
    async def manager():
        entered.append(asyncio.current_task())
        try:
            yield Environment(url="wss://127.0.0.1:1/exec", cwd="/workspace", connect_timeout=0.15)
        finally:
            assert len(delayed_native_join) >= 3
            exited.append(asyncio.current_task())

    child = Agent(instructions="Inspect.", model="gpt-5.5", provider=inner_provider)

    @function_tool(name="child")
    async def inspect(input: str) -> str:
        result = await child.run(input, environment=manager())
        return result.final_output

    parent = Agent(
        instructions="Inspect.", model="gpt-5.5", tools=[inspect], provider=outer_provider
    )
    responses.respond(function_call("child", {"input": "inspect"}, "child-1"))
    async with timeout(20):
        try:
            with pytest.raises(RuntimeError) as original:
                await asyncio.to_thread(parent.run_sync, "Inspect.")
            assert "controlled native retirement" not in str(original.value)
            assert len(entered) == 1 and not exited
            assert entered[0] is not None
            owner_loop = entered[0].get_loop()
            assert not owner_loop.is_running() and not owner_loop.is_closed()
            await outer_provider.aclose()
            assert not exited and not owner_loop.is_closed()
            await inner_provider.aclose()
            assert entered == exited and owner_loop.is_closed()
            await inner_provider.aclose()
            assert entered == exited
        finally:
            await outer_provider.aclose()
            await inner_provider.aclose()


@pytest.mark.parametrize("call_route", ["async", "sync"])
@pytest.mark.parametrize("implicit_provider", [True, False], ids=["implicit", "borrowed"])
@pytest.mark.parametrize("mcp_credentials", [False, True])
@pytest.mark.parametrize("delayed_native_join", [3], indirect=True)
async def test_interrupted_opening_exposes_existing_provider_cleanup(
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
    delayed_native_join: list[str],
    call_route: str,
    implicit_provider: bool,
    mcp_credentials: bool,
) -> None:
    from _agents_codex import session as native_session
    from agents import _native

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-interrupted-opening-test")
    retained = []
    entered = []
    manager_interrupted = asyncio.Event()
    original_engine = _native.create_engine
    original_cleanup = native_session._close_after_error
    original_defer = NativeProvider._defer_opening

    def defer(self, opening, environment):
        retained.append(self)
        return original_defer(self, opening, environment)

    def engine(*args, **kwargs):
        operation = original_engine(*args, **kwargs)

        async def publish():
            # Keep the real native result unpublished until the manager's
            # own timeout interrupts opening, independent of TCP refusal speed.
            await manager_interrupted.wait()
            return await operation

        return publish()

    async def observe_interruption(error, close):
        if (
            entered
            and asyncio.current_task() is entered[0]
            and isinstance(error, asyncio.CancelledError)
        ):
            manager_interrupted.set()
        await original_cleanup(error, close)

    async def forbid_submission(*args, **kwargs):
        pytest.fail("failed Environment startup must not submit a model request")

    monkeypatch.setattr(NativeProvider, "_defer_opening", defer)
    monkeypatch.setattr(_native, "create_engine", engine)
    monkeypatch.setattr(
        f"{native_session._EnvironmentLease.__module__}._close_after_error", observe_interruption
    )
    monkeypatch.setattr(_native.NativeThread, "_submit", forbid_submission)
    shared = None if implicit_provider else provider(responses)
    agent = Agent(
        instructions="Inspect.",
        model="gpt-5.5",
        provider=shared,
        tools=[
            HttpMCP(
                name="remote",
                url="https://example.invalid",
                tools=[],
                headers={"X-Token": "synthetic-cleanup-test-credential"},
            )
        ]
        if mcp_credentials
        else [],
    )

    @asynccontextmanager
    async def manager():
        entered.append(asyncio.current_task())
        async with timeout(0.02):
            yield Environment(url="wss://127.0.0.1:1/exec", cwd="/workspace", connect_timeout=0.15)

    async with timeout(20):
        try:
            with pytest.raises(RuntimeError, match="controlled native retirement") as caught:
                if call_route == "sync":
                    await asyncio.to_thread(agent.run_sync, "Inspect", environment=manager())
                else:
                    await agent.run("Inspect", environment=manager())
            error = caught.value
            assert len(retained) == 1
            recovery = get_cleanup(error)
            assert recovery is retained[0]
            assert not vars(recovery).get("_closed", False)
            assert error.__cause__ is None and error.__suppress_context__
            if implicit_provider:
                assert getattr(error, "provider", None) is recovery
                assert delayed_native_join == ["join"] * 3
            else:
                assert recovery is shared
                assert getattr(error, "provider", None) is None
                assert delayed_native_join == ["join"] * 2
                with pytest.raises(RuntimeError, match="controlled native retirement"):
                    await recovery.aclose()
            owner_loop = entered[0].get_loop()
            if call_route == "sync":
                assert not owner_loop.is_running() and not owner_loop.is_closed()
            # Native retirement now succeeds. The same manager's timeout still
            # propagates once before an idempotent retry completes the provider.
            with pytest.raises(asyncio.TimeoutError):
                await recovery.aclose()
            await recovery.aclose()
            await recovery.aclose()
            assert vars(recovery).get("_closed") is True
            assert delayed_native_join == ["join"] * 4
            assert get_cleanup(error) is recovery
            if call_route == "sync":
                assert owner_loop.is_closed()
        finally:
            for recovery in retained:
                for _ in range(3):
                    try:
                        await recovery.aclose()
                    except (RuntimeError, asyncio.TimeoutError):
                        continue
                    break
