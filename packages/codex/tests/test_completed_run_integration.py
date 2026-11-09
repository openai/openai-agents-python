"""Public activation/fallback checks and narrow Python handoff policy seams."""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from dataclasses import replace
from typing import Any

import pytest

from _agents_codex._completed_turn_owner import _CompletedTurnOwner
from _agents_codex.session import _join_run_owner
from agents import Agent, NativeProvider, Session
from agents.observation import TurnFinished
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, responses as responses
from tests.test_native_agent_observation import text_response


@pytest.mark.asyncio
async def test_success_uses_completed_owner_when_runtime_services_are_shared(
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
            if sys.platform == "linux":
                native_only.setattr(Session, "__init__", reject_session)
            result = await agent.run("eligible native scope")
        expected_owner = _CompletedTurnOwner if sys.platform == "linux" else Session
        assert isinstance(result.turn._session, expected_owner)
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


@pytest.mark.parametrize(
    "input",
    [None, {"role": "user", "content": "text"}, "x" * 16385],
    ids=["no-input", "item-input", "large-input"],
)
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
async def test_completed_facts_preserve_privacy_and_independent_reader_copies(mode: str) -> None:
    import json
    from dataclasses import asdict

    from agents.observation import EventGap, ResponseFactReceived
    from tests._asyncio import timeout
    from tests.test_native_response_facts import PRIVATE, fact_peer

    async with timeout(30), fact_peer() as peer:
        peer.replies.append((False, False))
        async with NativeProvider(
            base_url=peer.url, api_key="completed-facts-key", response_mode=mode
        ) as provider:
            result = await Agent(
                instructions="Answer.",
                model="gpt-5.2",
                provider=provider,
                model_settings={"top_logprobs": 0},
            ).run("answer")
        expected_owner = _CompletedTurnOwner if sys.platform == "linux" else Session
        assert isinstance(result.turn._session, expected_owner)
        assert result.final_output == "aa"
        assert len(peer.requests) == 1 and peer.requests[0]["top_logprobs"] == 0
        assert "message.output_text.logprobs" in peer.requests[0]["include"]
        ordinary = [event async for event in result.turn.events()]
        trusted = [event async for event in result.turn.events(include_execution_details=True)]
        assert PRIVATE not in json.dumps([asdict(event) for event in ordinary])
        assert PRIVATE not in repr(trusted)
        facts = [event.record for event in trusted if isinstance(event, ResponseFactReceived)]
        assert facts and PRIVATE in json.dumps(facts)
        assert all(row["thread_id"] == result.turn.session_id for row in facts)
        assert all(row["turn_id"] == result.turn.turn_id for row in facts)
        assert all(row["response_id"] == "response-1" for row in facts)
        assert all(row["upstream_request_id"] == "upstream-1" for row in facts)
        assert not any(isinstance(event, EventGap) for event in trusted)
        assert [event.sequence for event in trusted] == list(range(1, len(trusted) + 1))
        assert sum(isinstance(event, TurnFinished) for event in trusted) == 1
        facts[0]["fact"].clear()
        again = [event async for event in result.turn.events(include_execution_details=True)]
        assert all(
            event.record["fact"] for event in again if isinstance(event, ResponseFactReceived)
        )


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


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_before_open", [False, True], ids=["success", "cancel-before-open"])
async def test_real_native_run_holds_original_acquisition_before_open_poll(
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
    cancel_before_open: bool,
) -> None:
    async with timeout(30):
        native = importlib.import_module("agents._native")
        prepare = native._prepare_run_conversation
        entered = asyncio.Event()
        release = asyncio.Event()
        first_cancel = asyncio.Event()
        second_cancel = asyncio.Event()
        preparations: list[None] = []
        cancellations: list[None] = []

        class Gate:
            def __init__(self, real: Any) -> None:
                self.real = real

            async def run(self) -> Any:
                entered.set()
                await release.wait()
                return await self.real.run()

            def cancel(self) -> None:
                self.real.cancel()
                cancellations.append(None)
                if len(cancellations) == 1:
                    first_cancel.set()
                elif len(cancellations) == 2:
                    second_cancel.set()

        def held_prepare(*args: Any, **kwargs: Any) -> Gate:
            real = prepare(*args, **kwargs)
            preparations.append(None)
            return Gate(real)

        original = NativeProvider(
            base_url=responses.url,
            api_key="held-original-test-key",
            supports_websockets=False,
            headers={"x-test-opening-owner": "original"},
        )
        future = NativeProvider(
            base_url=responses.url,
            api_key="held-future-test-key",
            supports_websockets=False,
            headers={"x-test-opening-owner": "future"},
        )
        original_instructions = "Original held declaration marker."
        future_instructions = "Future replacement declaration marker."
        original_input = "Original held input marker."
        future_input = "Independent future input marker."
        agent = Agent(model="gpt-5.2", instructions=original_instructions, provider=original)
        running: asyncio.Task[Any] | None = None
        try:
            if not cancel_before_open:
                responses.respond(*text_response("held-original", "original answer"))
            with monkeypatch.context() as held:
                held.setattr(native, "_prepare_run_conversation", held_prepare)
                running = asyncio.create_task(agent.run(original_input))
                await entered.wait()
                assert preparations == [None]
                assert responses.requests == []  # Opening has not been polled.
                with pytest.raises(RuntimeError, match="open or opening Sessions"):
                    await original.aclose()

                # Create a new immutable declaration; the issued call keeps its capture.
                agent = replace(agent, instructions=future_instructions, provider=future)
                if cancel_before_open:
                    running.cancel("first held-opening cancellation")
                    await first_cancel.wait()
                    running.cancel("second held-opening cancellation")
                    await second_cancel.wait()
                    assert cancellations == [None, None]
                    assert not running.done()
                    assert responses.requests == []
                    release.set()
                    with pytest.raises(asyncio.CancelledError) as cancelled:
                        await running
                    assert cancelled.value.args == ("first held-opening cancellation",)
                    assert responses.requests == []
                else:
                    release.set()
                    completed = await running
                    assert completed.final_output == "original answer"
                    assert len(responses.requests) == 1
                    original_wire = json.dumps(responses.requests[0]["input"])
                    assert original_instructions in original_wire
                    assert original_input in original_wire
                    assert future_instructions not in original_wire
                    assert responses.request_headers[0]["authorization"] == (
                        "Bearer held-original-test-key"
                    )
                    assert responses.request_headers[0]["x-test-opening-owner"] == "original"

            # The original acquisition has joined before another declaration runs.
            await original.aclose()
            responses.respond(*text_response("held-future", "future answer"))
            completed = await agent.run(future_input)
            assert completed.final_output == "future answer"
            assert len(responses.requests) == (1 if cancel_before_open else 2)
            future_wire = json.dumps(responses.requests[-1]["input"])
            assert future_instructions in future_wire
            assert future_input in future_wire
            assert original_instructions not in future_wire
            assert original_input not in future_wire
            assert "original answer" not in future_wire
            assert responses.request_headers[-1]["authorization"] == "Bearer held-future-test-key"
            assert responses.request_headers[-1]["x-test-opening-owner"] == "future"
        finally:
            release.set()
            if running is not None:
                if not running.done():
                    running.cancel("held-opening test cleanup")
                await asyncio.gather(running, return_exceptions=True)
            try:
                await original.aclose()
            finally:
                await future.aclose()
