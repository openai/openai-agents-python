"""run_sync must return even when its native orphan still owns a manager."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager

import pytest

from agents import Agent, Environment, NativeProvider, function_tool
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
