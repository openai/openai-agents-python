"""Project a frozen native steering receipt without claiming to reproduce Core's final gap.

These tests change one private field on a real root terminal. They exercise Session
precedence and lifetime; the native receipt tests cover when that field is true.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agents import (
    Agent,
    NativeError,
    NativeProvider,
    Session,
    TurnStoppedError,
    function_tool,
)
from agents.observation import TurnFinished
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.review_ii_helpers import join_interrupt_cleanup


def provider(responses: ResponsesFixture) -> NativeProvider:
    return NativeProvider(base_url=responses.url, api_key="synthetic-steering-tail")


class InjectFirstFrozenTail:
    """Delegate real native operations, but mark the first real root terminal."""

    def __init__(self, native: Any) -> None:
        self.native = native
        self.terminal: tuple[str, str] | None = None
        self.delivered = asyncio.Event()
        self.pause = False
        self.release = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.native, name)

    async def _next_session_event(self) -> str:
        raw = await self.native._next_session_event()
        event = json.loads(raw)
        if event is not None and self.terminal is None and event.get("subagent_id") is None:
            message = event["msg"]
            kind = message["type"]
            if kind in {"task_complete", "turn_complete", "turn_aborted"}:
                turn_id = message.get("turn_id", message.get("turnId")) or event["id"]
                self.terminal = (turn_id, kind)
                # Test only the Python projection, keeping native identity and content.
                event["sdk_unsampled_steering"] = True
                self.delivered.set()
                if self.pause:
                    await self.release.wait()
                return json.dumps(event)
        return raw


class ProjectContinuation(InjectFirstFrozenTail):
    """Project a successful conditional admission through a real second turn.

    The injected flag has no real native steering tail. Adapt only that
    conditional admission here; Rust's controlled engine-gap test verifies
    retained input, authority and the actual internal continuation fragment.
    """

    async def _submit(self, encoded: str, *, model_attempts: Any = None) -> str:
        request = json.loads(encoded)
        if request["mode"] == "continue_if_idle":
            assert self.terminal is not None
            assert request["expected_previous_turn_id"] == self.terminal[0]
            assert request["input"] == []
            assert model_attempts is None
            request = {
                "mode": "start_if_idle",
                "input": [{"type": "text", "text": "synthetic continuation admission"}],
                "start": request.get("start", {}),
            }
        encoded = json.dumps(request)
        if model_attempts is None:
            return await self.native._submit(encoded)
        return await self.native._submit(encoded, model_attempts=model_attempts)


async def test_frozen_tail_preserves_exact_turn_and_follows_only_its_automatic_successor(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    effects: list[str] = []

    @function_tool
    async def record(label: str) -> str:
        effects.append(label)
        return f"recorded:{label}"

    responses.respond(function_call("record", {"label": "record-a"}, "only-effect"))
    responses.respond(assistant_output("first native result"))
    responses.respond(assistant_output("automatic native result"))
    responses.respond(assistant_output("explicit later result"))
    continuation_started, release_continuation = asyncio.Event(), asyncio.Event()

    async def hold_continuation(event: dict[str, Any]) -> None:
        if event["type"] == "response.created" and event["response"]["id"] == "resp_3":
            continuation_started.set()
            await release_continuation.wait()

    responses.before_event = hold_continuation
    async with (
        timeout(30),
        Agent(
            instructions="Call record, then answer.", tools=[record], provider=provider(responses)
        ).session() as session,
    ):
        stream = session.events()
        proxy = ProjectContinuation(session._native)
        proxy.pause = True
        monkeypatch.setattr(session, "_native", proxy)
        first = await session.submit("keep this ordinary user history: cobalt")
        following = asyncio.create_task(first.get_result())
        try:
            await proxy.delivered.wait()
            assert not following.done()
            assert proxy.terminal is not None and proxy.terminal[0] == first.turn_id
            assert len(responses.requests) == 2
            proxy.release.set()
            await continuation_started.wait()
            assert not first._root_result_ready()
            release_continuation.set()
            automatic = await first.get_result()
            assert first._root_result_ready()
            assert automatic is await following
            assert automatic.final_output == "automatic native result"
            assert automatic.turn is first
            history = [item async for item in session.items()]
            assert "keep this ordinary user history: cobalt" in json.dumps(history)
            assert (
                sum(
                    item["type"] == "function_call_output" and item.get("call_id") == "only-effect"
                    for item in history
                )
                == 1
            )
            second = await session.submit("explicit later input")
            assert (await second.get_result()).final_output == "explicit later result"
            assert await first.get_result() is automatic
            assert effects == ["record-a"] and len(responses.requests) == 4
        finally:
            proxy.release.set()
            release_continuation.set()
            await asyncio.gather(following, return_exceptions=True)
    assert await first.get_result() is automatic
    finished = [event async for event in stream if isinstance(event, TurnFinished)]
    assert len(finished) == 3
    assert [event.status for event in finished] == ["completed"] * 3
    assert finished[0].turn_id == first.turn_id and finished[2].turn_id == second.turn_id
    assert finished[1].turn_id not in (first.turn_id, second.turn_id)


async def test_frozen_flag_without_native_tail_cannot_authorize_a_successor(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses.respond(assistant_output("exact original result"))
    responses.respond(assistant_output("explicit later result"))
    async with (
        timeout(20),
        Agent(instructions="Answer.", provider=provider(responses)).session() as s,
    ):
        monkeypatch.setattr(s, "_native", InjectFirstFrozenTail(s._native))
        first = await s.submit("original input")
        with pytest.raises(NativeError, match="[Ss]uperseded"):
            await first.get_result()
        with pytest.raises(NativeError, match="[Ss]uperseded"):
            await first.get_result()
        assert len(responses.requests) == 1
        second = await s.submit("explicit later input")
        assert (await second.get_result()).final_output == "explicit later result"
    assert len(responses.requests) == 2


@pytest.mark.parametrize("with_handler", [False, True])
async def test_one_shot_joins_the_identified_automatic_successor(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, with_handler: bool
) -> None:
    responses.respond(assistant_output("original result"))
    responses.respond(assistant_output("automatic result"))
    proxies: list[ProjectContinuation] = []
    start_readers = Session._start_readers

    def project(session):
        proxy = ProjectContinuation(session._native)
        proxies.append(proxy)
        session._native = proxy
        start_readers(session)

    monkeypatch.setattr(Session, "_start_readers", project)
    agent = Agent(instructions="Answer.", provider=provider(responses))
    async with timeout(20):
        result = await agent.run(
            "original input",
            # This fixture injects Session events; use its explicit tool-roster path.
            tools=[],
            on_interaction=(lambda request: request.reply("unused")) if with_handler else None,
        )
    assert result.final_output == "automatic result"
    assert proxies[0].terminal is not None
    assert result.turn.turn_id == proxies[0].terminal[0]
    assert len(responses.requests) == 2


async def test_real_native_failure_wins_over_frozen_tail_and_session_recovers(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses.respond(
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_prompt",
                    "message": "synthetic request rejected",
                }
            },
        }
    )
    responses.respond(assistant_output("recovered"))
    agent = Agent(instructions="Answer.", provider=provider(responses))
    async with timeout(30), agent.session() as session:
        proxy = InjectFirstFrozenTail(session._native)
        monkeypatch.setattr(session, "_native", proxy)
        first = await session.submit("rejected request")
        with pytest.raises(NativeError, match="synthetic request rejected") as caught:
            await first.get_result()
        assert proxy.terminal is not None
        assert proxy.terminal[0] == first.turn_id
        assert proxy.terminal[1] in {"task_complete", "turn_complete"}
        second = await session.submit("try again")
        assert (await second.get_result()).final_output == "recovered"
        with pytest.raises(NativeError) as repeated:
            await first.get_result()
        assert repeated.value is caught.value
    assert len(responses.requests) == 2


async def test_real_abort_wins_over_frozen_tail_and_session_recovers(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, finished = asyncio.Event(), asyncio.Event()
    effects: list[str] = []

    @function_tool
    async def blocked() -> str:
        """Wait until the owning turn stops."""
        started.set()
        try:
            await asyncio.Event().wait()
            effects.append("must not run")
            return "must not complete"
        finally:
            finished.set()

    responses.respond(function_call("blocked", {}, "abort-once"))
    responses.respond(assistant_output("fresh root"))
    agent = Agent(
        instructions="Use blocked.",
        tools=[blocked],
        provider=provider(responses),
    )
    async with timeout(30), agent.session() as session:
        proxy = InjectFirstFrozenTail(session._native)
        monkeypatch.setattr(session, "_native", proxy)
        first = await session.submit("stop this real callback")
        await started.wait()
        await first.cancel()
        with pytest.raises(TurnStoppedError):
            await first.get_result()
        assert finished.is_set()
        assert proxy.terminal == (first.turn_id, "turn_aborted")
        await join_interrupt_cleanup(session)
        second = await session.submit("next turn")
        assert (await second.get_result()).final_output == "fresh root"
        with pytest.raises(TurnStoppedError):
            await first.get_result()
    assert effects == []
    assert len(responses.requests) == 2
