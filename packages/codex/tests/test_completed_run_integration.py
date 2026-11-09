"""Public activation/fallback checks and narrow Python handoff policy seams."""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

import pytest

from _agents_codex._completed_turn_owner import _CompletedTurnOwner
from _agents_codex.session import _join_run_owner
from agents import Agent, NativeProvider, Session
from agents.observation import TurnFinished
from tests.native_fixtures import responses as responses
from tests.test_native_agent_observation import text_response


@pytest.mark.asyncio
async def test_success_returns_real_completed_without_constructing_session(
    responses: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("_agents_codex.agent")
    predicate = module._completed_run_eligible
    provider = NativeProvider(base_url=responses.url, api_key="completed-integration-key")
    agent = Agent(instructions="Answer plainly.", provider=provider)
    try:
        responses.replies.append(text_response("warm", "warm"))
        with monkeypatch.context() as ordinary:
            ordinary.setattr(module, "_completed_run_eligible", lambda *args: False)
            warm = await agent.run("warm ordinary atomic scope")
            assert isinstance(warm.turn._session, Session)
        assert module._completed_run_eligible is predicate
        responses.replies.append(text_response("native", "answer"))

        def reject_session(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("Supported completed run constructed a live Session")

        with monkeypatch.context() as native_only:
            native_only.setattr(Session, "__init__", reject_session)
            result = await agent.run("eligible native scope")
        assert isinstance(result.turn._session, _CompletedTurnOwner)
        assert result.final_output == "answer"
        assert await result.turn.get_result() is result
        assert len(responses.requests) == 2
        rows = [row async for row in result.turn.events()]
        assert sum(isinstance(row, TurnFinished) for row in rows) == 1
        assert [row.sequence for row in rows] == sorted({row.sequence for row in rows})
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_blank_final_hands_actual_admission_and_metadata_to_session(
    responses: Any,
) -> None:
    provider = NativeProvider(base_url=responses.url, api_key="completed-blank-key")
    try:
        responses.replies.append(text_response("blank", ""))
        result = await Agent(instructions="Answer plainly.", provider=provider).run("blank")
        assert result.final_output == ""
        session = result.turn._session
        assert isinstance(session, Session)
        assert session._closed and session._reader.done()
        assert len(responses.requests) == 1  # A transferred admission must never resubmit.
        rows = [row async for row in result.turn.events()]
        assert sum(type(row).__name__ == "ModelRequestDispatching" for row in rows) == 1
        assert sum(type(row).__name__ == "ModelResponseLinked" for row in rows) == 1
        assert sum(isinstance(row, TurnFinished) for row in rows) == 1
        assert len({row.sequence for row in rows}) == len(rows)
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_large_output_handoff_preserves_output_and_single_request(responses: Any) -> None:
    text = "x" * (512 * 1024)
    provider = NativeProvider(base_url=responses.url, api_key="completed-large-key")
    try:
        responses.replies.append(text_response("large", text))
        result = await Agent(instructions="Answer plainly.", provider=provider).run("large")
        assert isinstance(result.turn._session, Session)
        assert result.final_output == text
        assert result.turn._session._closed
        assert len(responses.requests) == 1
        assert await result.turn.get_result() is result
        assert [row async for row in result.turn.events()]
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_eligibility_opt_out_keeps_existing_atomic_opening(
    responses: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("_agents_codex.agent")
    native = importlib.import_module("agents._native")
    original = native._open_conversation
    opens = []

    def atomic(*args: Any, **kwargs: Any) -> Any:
        opens.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(native, "_open_conversation", atomic)
    monkeypatch.setattr(module, "_completed_run_eligible", lambda *args: False)
    provider = NativeProvider(base_url=responses.url, api_key="completed-control-key")
    try:
        responses.replies.append(text_response("ordinary", "answer"))
        result = await Agent(instructions="Answer plainly.", provider=provider).run("ordinary")
        assert isinstance(result.turn._session, Session)
        assert opens == [1]
    finally:
        await provider.aclose()


@pytest.mark.parametrize("input", [None, {"role": "user", "content": "text"}, "x" * 16385])
def test_unsupported_inputs_do_not_select_completed_route(input: Any) -> None:
    from _agents_codex.agent import _completed_run_eligible

    assert not _completed_run_eligible(
        Agent(instructions="Answer."), input, None, False, None, None
    )


def test_context_state_return_and_tracing_do_not_select_completed_route() -> None:
    from _agents_codex.agent import _completed_run_eligible

    agent = Agent(instructions="Answer.")
    assert not _completed_run_eligible(agent, "text", object(), False, None, None)
    assert not _completed_run_eligible(agent, "text", None, True, None, None)
    assert not _completed_run_eligible(agent, "text", None, False, None, {})


@pytest.mark.asyncio
async def test_native_owner_factory_failure_keeps_original_exception() -> None:
    problem = RuntimeError("factory failed before opening")

    class Owner:
        def run(self) -> Any:
            raise problem

    with pytest.raises(RuntimeError) as caught:
        await _join_run_owner(Owner())
    assert caught.value is problem


@pytest.mark.asyncio
async def test_delivery_cancellation_joins_owner_and_keeps_first_message() -> None:
    started, release = asyncio.Event(), asyncio.Event()
    stops = []

    class Owner:
        def run(self) -> Any:
            async def operation() -> str:
                started.set()
                await release.wait()
                return "exact outcome"

            return operation()

        def cancel(self) -> None:
            stops.append(1)

    task = asyncio.create_task(_join_run_owner(Owner()))
    await started.wait()
    task.cancel("first cancellation")
    await asyncio.sleep(0)
    task.cancel("later cancellation")
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    outcome, cancellation = await task
    assert outcome == "exact outcome"
    assert cancellation is not None and cancellation.args == ("first cancellation",)
    assert stops


@pytest.mark.asyncio
async def test_implicit_provider_stays_on_ordinary_owned_cleanup(
    responses: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("_agents_codex.session")
    native = importlib.import_module("agents._native")
    providers = []

    class LocalOwnedProvider(NativeProvider):
        def __init__(self) -> None:
            super().__init__(base_url=responses.url, api_key="implicit-completed-key")
            providers.append(self)

    def reject_native_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Implicit owned Provider entered native completed route")

    monkeypatch.setattr(module, "NativeProvider", LocalOwnedProvider)
    monkeypatch.setattr(native, "_prepare_run_conversation", reject_native_run)
    responses.replies.append(text_response("implicit", "answer"))
    try:
        result = await Agent(instructions="Answer plainly.").run("implicit provider")
        assert isinstance(result.turn._session, Session)
        assert result.final_output == "answer"
        assert len(providers) == 1 and providers[0]._closed
        assert result.turn._session._owned_provider is None
        assert len(responses.requests) == 1
    finally:
        for provider in providers:
            await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["streaming", "buffered"])
async def test_refusal_fallback_preserves_typed_fact_privacy_and_error(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    from dataclasses import asdict

    from agents import NativeError
    from agents.observation import ResponseFactReceived
    from tests._asyncio import timeout
    from tests.test_native_response_facts import PRIVATE, fact_peer

    opened = []
    original = Session._open.__func__

    async def capture(cls: Any, *args: Any, **kwargs: Any) -> Any:
        result = await original(cls, *args, **kwargs)
        if isinstance(result, Session):
            opened.append(result)
        return result

    monkeypatch.setattr(Session, "_open", classmethod(capture))
    async with fact_peer() as peer:
        peer.replies.append((True, False))
        provider = NativeProvider(
            base_url=peer.url, api_key="completed-refusal-key", response_mode=mode
        )
        try:
            agent = Agent(instructions="Answer.", model="gpt-5.2", provider=provider)
            async with timeout(30):
                with pytest.raises(NativeError) as caught:
                    await agent.run("refuse")
            assert caught.value.response_integrity_reason == "provider_refusal"
            assert PRIVATE not in str(caught.value)
            assert len(opened) == 1 and opened[0]._closed
            turn = opened[0]._last_turn
            assert turn is not None
            ordinary = [event async for event in turn.events()]
            trusted = [event async for event in turn.events(include_execution_details=True)]
            assert PRIVATE not in json.dumps([asdict(event) for event in ordinary])
            assert PRIVATE in json.dumps(
                [event.record for event in trusted if isinstance(event, ResponseFactReceived)]
            )
            assert PRIVATE not in repr(trusted)
            assert len(peer.requests) == 1
        finally:
            await provider.aclose()


@pytest.mark.asyncio
async def test_terminal_transfer_reports_scope_failure_once_and_preserves_completed(
    responses: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Policy seam only: injected failure receipt follows real joined shutdown.

    This does not qualify native partial retirement or the Opening lease barrier.
    The copied real Session has already released its lease; retry calls are real
    idempotent closes on its original closed native owners.
    """
    import copy
    import weakref

    from _agents_codex.observation import _ObservationBuffer

    module = importlib.import_module("_agents_codex.agent")
    monkeypatch.setattr(module, "_completed_run_eligible", lambda *args: False)
    provider = NativeProvider(base_url=responses.url, api_key="completed-close-seam")
    try:
        responses.replies.append(text_response("close-seam", "answer"))
        original = await Agent(instructions="Answer.", provider=provider).run("close")
        session = copy.copy(original.turn._session)
        session._closed = session._closing = session._resources_retired = False
        session._scope_close_task = session._close_task = None
        session._native_scope_failure = None
        session._last_turn = None
        session._observations = _ObservationBuffer(session.id)
        session._observations._callback_check = weakref.WeakMethod(
            session._check_callback_operation
        )
        session._request_observation_routes = weakref.WeakValueDictionary()
        closes = []

        class ClosedOwner:
            def __init__(self, inner: Any, phase: str) -> None:
                self.inner, self.phase = inner, phase

            def __getattr__(self, name: str) -> Any:
                return getattr(self.inner, name)

            async def close(self) -> None:
                closes.append(self.phase)
                await self.inner.close()

        class Archive:
            def json(self) -> str:
                return "[]"

        session._native = ClosedOwner(session._native, "thread")
        session._engine = ClosedOwner(session._engine, "engine")
        thread_error = RuntimeError("injected thread failure after shutdown")
        engine_error = RuntimeError("injected engine failure after shutdown")
        state = session._adopt_run(
            {
                "status": "retirement_failed",
                "receipt": {"status": "started", "turn_id": original.turn.turn_id},
                "output": "answer",
                "usage": None,
                "retired": {"thread": False, "engine": False},
            },
            Archive(),
            [("thread", thread_error), ("engine", engine_error)],
            [{"type": "text", "text": "close"}],
            "close-transfer-submission",
        )
        turn = state.handle
        assert turn is not None
        completed = await turn.get_result()
        assert completed.final_output == "answer"
        with pytest.raises(RuntimeError) as caught:
            await session.aclose()
        assert caught.value is engine_error
        assert thread_error in _contexts(engine_error)
        assert engine_error.cleanup is session
        assert not closes
        assert await turn.get_result() is completed
        rows = [row async for row in turn.events()]
        assert [row.status for row in rows if isinstance(row, TurnFinished)] == ["completed"]
        await session.aclose()
        assert closes == ["thread", "engine"]
        assert session._closed
    finally:
        await provider.aclose()


def _contexts(error: BaseException) -> list[BaseException]:
    result = []
    while error.__context__ is not None and error.__context__ not in result:
        error = error.__context__
        result.append(error)
    return result


@pytest.mark.asyncio
async def test_environment_change_hands_unsubmitted_scope_to_ordinary_session(
    responses: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = NativeProvider(base_url=responses.url, api_key="completed-environment-key")
    agent = Agent(instructions="Answer plainly.", provider=provider)
    try:
        responses.replies.append(text_response("before-env", "before"))
        first = await agent.run("prime original environment")
        monkeypatch.setenv("OPENAI_AGENTS_COMPLETED_OWNER_TEST_BINDING", "changed-between-runs")
        responses.replies.append(text_response("after-env", "after"))
        second = await agent.run("fresh changed environment")
        assert first.final_output == "before" and second.final_output == "after"
        assert first.turn.session_id != second.turn.session_id
        assert first.turn.turn_id != second.turn.turn_id
        assert isinstance(second.turn._session, Session)
        assert second.turn._session._closed and second.turn._session._reader.done()
        assert second.turn._session._provider_lease is None
        assert len(responses.requests) == 2
    finally:
        await provider.aclose()


@pytest.mark.parametrize("hook", ["transform_context", "before_compact", "before_complete"])
def test_context_and_completion_hooks_opt_out_before_native_run(hook: str) -> None:
    from _agents_codex.agent import _completed_run_eligible

    agent = Agent(instructions="Answer.", **{hook: lambda *args, **kwargs: None})
    assert not _completed_run_eligible(agent, "text", None, False, None, None)


@pytest.mark.asyncio
async def test_unsubmitted_publication_error_joins_placeholder_and_allows_reuse(
    responses: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    provider = NativeProvider(base_url=responses.url, api_key="completed-opening-failure")
    agent = Agent(instructions="Answer plainly.", provider=provider)
    original = Session._publish_opening_configuration
    problem = RuntimeError("synthetic opening publication failure")
    failed = []
    homes = []

    async def fail_once(self: Any, **kwargs: Any) -> None:
        if not failed:
            failed.append(self)
            assert self._home is not None  # The environment mismatch owns a dedicated scope.
            homes.append(Path(self._home.name))
            raise problem
        await original(self, **kwargs)

    try:
        responses.replies.append(text_response("prime-publication", "prime"))
        assert (await agent.run("prime original environment")).final_output == "prime"
        monkeypatch.setenv("OPENAI_AGENTS_COMPLETED_OWNER_PUBLICATION_TEST", "changed")
        monkeypatch.setattr(Session, "_publish_opening_configuration", fail_once)
        responses.replies.append(text_response("after-publication", "recovered"))
        with pytest.raises(RuntimeError) as caught:
            await agent.run("opening must fail before sampling")
        assert caught.value is problem
        assert len(failed) == 1 and failed[0]._reader.done()
        assert all(not home.exists() for home in homes)
        assert len(responses.requests) == 1
        result = await agent.run("fresh recovery")
        assert result.final_output == "recovered"
        assert isinstance(result.turn._session, Session)
        assert result.turn._session._closed and result.turn._session._reader.done()
        assert len(responses.requests) == 2
    finally:
        await provider.aclose()
