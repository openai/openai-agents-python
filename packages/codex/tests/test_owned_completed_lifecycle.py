"""Owned-provider completion and handoff; loopback injection only changes routing."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from typing import Any

import pytest

from _agents_codex._lifecycle import _error_contexts
from agents import Agent, NativeProvider, Session
from agents.observation import TurnFinished
from tests._asyncio import capture_cancellation, timeout
from tests.native_fixtures import ResponsesFixture, responses as responses
from tests.test_native_agent_observation import text_response

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Shared native Completed scope")


@pytest.fixture
def owned_providers(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> list[NativeProvider]:
    providers: list[NativeProvider] = []

    class LocalOwnedProvider(NativeProvider):
        def __init__(self) -> None:
            super().__init__(base_url=responses.url, supports_websockets=False)
            providers.append(self)

    monkeypatch.setenv("OPENAI_API_KEY", "owned-completed-lifecycle-key")
    monkeypatch.setattr(
        importlib.import_module("_agents_codex.session"), "NativeProvider", LocalOwnedProvider
    )
    return providers


@pytest.fixture
def held_native_result(monkeypatch: pytest.MonkeyPatch) -> Any:
    native = importlib.import_module("agents._native")
    prepare = native._prepare_run_conversation
    observed: dict[str, Any] = {}
    ready, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Owner:
        def __init__(self, actual: Any) -> None:
            self.actual = actual

        async def run(self) -> Any:
            result = await self.actual.run()
            observed["scope"], info, _, observed["failures"] = result
            observed["info"] = json.loads(info)
            ready.set()
            await release.wait()
            return result

        def cancel(self) -> None:
            self.actual.cancel()
            stopped.set()

    monkeypatch.setattr(
        native, "_prepare_run_conversation", lambda *a, **k: Owner(prepare(*a, **k))
    )
    return observed, ready, release, stopped


@pytest.mark.parametrize("decline", [False, True], ids=["live-prefix", "unsubmitted"])
async def test_owned_handoff_preserves_one_submission_and_joined_close(
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
    owned_providers: list[NativeProvider],
    held_native_result: Any,
    decline: bool,
) -> None:
    observed, _, release, _ = held_native_result
    release.set()
    if decline:
        native = importlib.import_module("agents._native")
        prepare = native._prepare_run_conversation

        def declined(*args: Any, **kwargs: Any) -> Any:
            values = list(args)
            params = json.loads(values[5])
            params["mode"] = "unsupported-collector-mode"
            values[5] = json.dumps(params)
            return prepare(*values, **kwargs)

        monkeypatch.setattr(native, "_prepare_run_conversation", declined)
    responses.replies.append(text_response("handoff", "answer" if decline else ""))
    try:
        async with timeout(20):
            result = await Agent(instructions="Answer plainly.").run("one owned input")
        assert observed["info"]["status"] == ("unsubmitted" if decline else "live")
        assert observed["scope"] is not None
        session = result.turn._session
        assert isinstance(session, Session)
        assert session._closed and session._resources_retired
        assert session._reader is not None and session._reader.done()
        assert session._provider_lease is None and session._owned_provider is None
        assert len(owned_providers) == 1 and owned_providers[0]._closed
        assert len(responses.requests) == 1
        assert result.final_output == ("answer" if decline else "")
        rows = [row async for row in result.turn.events()]
        assert sum(isinstance(row, TurnFinished) for row in rows) == 1
    finally:
        for provider in owned_providers:
            await provider.aclose()


@pytest.mark.parametrize("fail_adoption", [False, True], ids=["native-success", "failed-adoption"])
async def test_owned_double_cancellation_preserves_existing_cleanup_owner(
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
    owned_providers: list[NativeProvider],
    held_native_result: Any,
    fail_adoption: bool,
) -> None:
    observed, ready, release, stopped = held_native_result
    actual_close = Session._close_session
    actual_provider_close = NativeProvider.aclose
    failed: list[Session[Any, Any]] = []
    provider_closes: list[NativeProvider] = []
    failure = RuntimeError("injected Session retirement failure")
    cancellations: list[asyncio.CancelledError] = []

    async def fail_once(session: Session[Any, Any]) -> None:
        if not failed:
            failed.append(session)
            raise failure
        await actual_close(session)

    async def close_provider(provider: NativeProvider) -> None:
        provider_closes.append(provider)
        await actual_provider_close(provider)

    if fail_adoption:
        monkeypatch.setattr(Session, "_close_session", fail_once)
    monkeypatch.setattr(NativeProvider, "aclose", close_provider)
    responses.replies.append(
        text_response("cancelled-owned", "z" * (96 * 1024) if fail_adoption else "answer")
    )
    task = asyncio.create_task(
        capture_cancellation(
            Agent(instructions="Keep one cancellation owner.").run("one owned request"),
            cancellations,
        )
    )
    try:
        async with timeout(20):
            await ready.wait()
            assert observed["info"]["status"] == ("live" if fail_adoption else "completed")
            assert (observed["scope"] is not None) is fail_adoption
            assert not observed["failures"]
            if not fail_adoption:
                assert all(observed["info"]["retired"].values())
            task.cancel("original owned cancellation")
            await stopped.wait()
            task.cancel("later owned cancellation")
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(cancellations) == 1
            primary = cancellations[0]
            assert primary.args == ("original owned cancellation",)
            if fail_adoption:
                cleanup = getattr(primary, "cleanup", None)
                assert failed and cleanup is failed[0]
                assert failure in _error_contexts(primary)
                assert not cleanup._closed and cleanup._provider_lease is not None
                assert cleanup._owned_provider is owned_providers[0]
                assert getattr(primary, "provider", None) is owned_providers[0]
                assert provider_closes == []
                await cleanup.aclose()
                assert cleanup._closed and cleanup._resources_retired
                assert cleanup._provider_lease is None
                assert cleanup._reader is not None and cleanup._reader.done()
            assert len(owned_providers) == 1 and owned_providers[0]._closed
            assert provider_closes == owned_providers
            assert len(responses.requests) == 1
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for session in failed:
            await session.aclose()
        for provider in owned_providers:
            await provider.aclose()


async def test_completed_provider_close_failure_keeps_retryable_provider_owner(
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
    owned_providers: list[NativeProvider],
) -> None:
    actual_close = NativeProvider.aclose
    failure = RuntimeError("injected owned Provider close failure")
    attempts = 0

    async def fail_twice(provider: NativeProvider) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise failure
        await actual_close(provider)

    monkeypatch.setattr(NativeProvider, "aclose", fail_twice)
    responses.replies.append(text_response("close-failure", "answer"))
    try:
        with pytest.raises(RuntimeError) as caught:
            await Agent(instructions="Answer plainly.").run("close this owned parent")
        assert caught.value is failure and attempts == 2
        assert len(owned_providers) == 1 and not owned_providers[0]._closed
        assert getattr(caught.value, "cleanup", None) is owned_providers[0]
        cleanup = owned_providers[0]
        await cleanup.aclose()
        assert owned_providers[0]._closed and len(responses.requests) == 1
    finally:
        for provider in owned_providers:
            await actual_close(provider)
