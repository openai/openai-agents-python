"""Verify native usage through real HTTP, session ownership and checkpoints."""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from typing_extensions import assert_type

from agents import (
    Agent,
    Completed,
    Session,
    SessionState,
    Turn,
    TurnStoppedError,
    function_tool,
)
from agents._history import project_history
from agents.observation import UsageUpdated
from agents.usage import TokenUsage, TokenUsageInfo
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent_observation import collect, provider


async def _report_usage(event: dict[str, Any]) -> None:
    if event["type"] == "response.completed":
        event["response"]["usage"] = {
            "input_tokens": 17,
            "input_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 4},
            "output_tokens": 7,
            "output_tokens_details": {"reasoning_tokens": 2},
            "total_tokens": 24,
            "fixture_secret": "not-for-usage-readers",
        }


async def _omit_usage(event: dict[str, Any]) -> None:
    if event["type"] == "response.completed":
        event["response"].pop("usage")


async def check_usage_types(session: Session[object, str], handle: Turn[str]) -> None:
    result = await (handle).get_result()
    assert_type(result, Completed[str])
    assert_type(result.thread_usage, TokenUsageInfo | None)
    if result.thread_usage is not None:
        assert_type(result.thread_usage.last_token_usage, TokenUsage)
        assert_type(result.thread_usage.model_context_window, int | None)
    async for event in handle.events():
        if event.type == "usage_updated":
            assert_type(event, UsageUpdated)
            assert_type(event.usage, TokenUsageInfo | None)


async def test_native_multiple_responses_turns_and_repeated_rate_observations(
    responses: ResponsesFixture,
) -> None:
    @function_tool
    def lookup(code: str) -> str:
        """Resolve a code."""
        return "resolved-" + code

    responses.before_event = _report_usage
    responses.respond(function_call("lookup", {"code": "A"}, "lookup-a"))
    responses.respond(assistant_output("first"))
    responses.respond(assistant_output("second"))
    responses.respond(assistant_output("no new counts"))
    agent = Agent(instructions="Use lookup once.", tools=[lookup], provider=provider(responses))
    async with timeout(30), agent.session() as session:
        first = await session.submit("look up A")
        first_result = await (first).get_result()
        assert first_result.final_output == "first"
        assert first_result.thread_usage is not None
        assert first_result.thread_usage.total_token_usage == TokenUsage(34, 6, 8, 14, 4, 48)
        assert first_result.thread_usage.last_token_usage == TokenUsage(17, 3, 4, 7, 2, 24)
        first_events = [
            event for event in await collect(first.events()) if isinstance(event, UsageUpdated)
        ]
        assert first_events
        assert first_events[0].usage is not None
        assert first_events[0].usage.total_token_usage == TokenUsage(17, 3, 4, 7, 2, 24)
        assert first_events[-1].usage == first_result.thread_usage
        assert all(event.session_id == session.id for event in first_events)
        assert all(event.turn_id == first.turn_id for event in first_events)
        assert all(event.submission_id == first.submission_id for event in first_events)
        assert "not-for-usage-readers" not in repr(first_events)
        assert "fixture_response" not in repr(first_result.thread_usage)
        with pytest.raises(FrozenInstanceError):
            first_result.thread_usage.total_token_usage.input_tokens = 0  # type: ignore[misc]

        second = await session.submit("next")
        second_result = await (second).get_result()
        assert second_result.thread_usage is not None
        assert second_result.thread_usage.total_token_usage == TokenUsage(51, 9, 12, 21, 6, 72)
        assert second_result.thread_usage.last_token_usage == TokenUsage(17, 3, 4, 7, 2, 24)
        second_events = [
            event for event in await collect(second.events()) if isinstance(event, UsageUpdated)
        ]
        assert second_events[-1].usage == second_result.thread_usage
        assert all(event.turn_id == second.turn_id for event in second_events)
        assert (await (first).get_result()) is first_result
        assert first_result.thread_usage.total_token_usage == TokenUsage(34, 6, 8, 14, 4, 48)
        assert len(responses.requests) == 3
        assert tool_output(responses.requests[1], "lookup-a") == "resolved-A"
        assert "first" in json.dumps(responses.requests[2]["input"])
        turns = {turn["id"]: turn async for turn in session.turns()}
        assert turns[first.turn_id]["usage"] == {
            "input_tokens": 34,
            "input_tokens_details": {"cached_tokens": 6, "cache_write_tokens": 8},
            "output_tokens": 14,
            "output_tokens_details": {"reasoning_tokens": 4},
            "total_tokens": 48,
        }
        first_responses = turns[first.turn_id]["responses"]
        assert [response["response_id"] for response in first_responses] == ["resp_1", "resp_2"]
        assert len({response["id"] for response in first_responses}) == 2
        assert all(response["thread_id"] == session.id for response in first_responses)
        assert all(response["turn_id"] == first.turn_id for response in first_responses)
        assert all(response["usage_source"] == "received" for response in first_responses)
        assert all(response.get("status") == "completed" for response in first_responses)
        assert all(
            response["usage"]
            == {
                "input_tokens": 17,
                "input_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 4},
                "output_tokens": 7,
                "output_tokens_details": {"reasoning_tokens": 2},
                "total_tokens": 24,
            }
            for response in first_responses
        )
        assert "not-for-usage-readers" not in repr(first_responses)
        assert turns[second.turn_id]["usage"] == {
            "input_tokens": 17,
            "input_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 4},
            "output_tokens": 7,
            "output_tokens_details": {"reasoning_tokens": 2},
            "total_tokens": 24,
        }

        # Native defers a rate observation until completion. No new usage
        # leaves its previous counts intact, even on successive submissions.
        responses.before_event = _omit_usage
        third = await session.submit("no usage reported")
        third_result = await (third).get_result()
        third_events = [
            event for event in await collect(third.events()) if isinstance(event, UsageUpdated)
        ]
        assert third_result.final_output == "no new counts"
        assert third_result.thread_usage == second_result.thread_usage
        assert third_events and all(
            event.usage == second_result.thread_usage for event in third_events
        )
        assert len(responses.requests) == 4
    assert first_result.thread_usage.total_token_usage == TokenUsage(34, 6, 8, 14, 4, 48)
    assert (await (second).get_result()).thread_usage == second_result.thread_usage


async def test_native_http_retry_counts_successful_usage_once() -> None:
    responses = ResponsesFixture(before_event=_report_usage)
    responses.respond(assistant_output("recovered"))
    attempts: list[dict[str, Any]] = []

    async def handle_request(request: web.Request) -> web.StreamResponse:
        attempts.append(await request.json())
        if len(attempts) == 1:
            return web.json_response(
                {"error": {"type": "server_error", "message": "controlled transient failure"}},
                status=503,
            )
        return await responses.handle(request)

    app = web.Application()
    app.router.add_post("/v1/responses", handle_request)
    async with timeout(30), TestServer(app) as server:
        responses.url = str(server.make_url("/v1"))
        agent = Agent(instructions="Answer.", provider=provider(responses))
        async with agent.session() as session:
            handle = await session.submit("retry the transient transport failure")
            completed = await (handle).get_result()
            assert completed.final_output == "recovered"
            assert completed.thread_usage is not None
            expected = TokenUsage(17, 3, 4, 7, 2, 24)
            assert completed.thread_usage.total_token_usage == expected
            assert completed.thread_usage.last_token_usage == expected
            observed = [
                event for event in await collect(handle.events()) if isinstance(event, UsageUpdated)
            ]
            assert observed and observed[-1].usage == completed.thread_usage
            assert all(event.usage in (None, completed.thread_usage) for event in observed)
            assert len(attempts) == 2
            assert attempts[0] == attempts[1]
            assert len(responses.requests) == 1


async def test_missing_native_usage_does_not_become_zero(responses: ResponsesFixture) -> None:
    responses.before_event = _omit_usage
    responses.respond(assistant_output("no counts"))
    async with (
        timeout(30),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        handle = await session.submit("one")
        completed = await (handle).get_result()
        assert completed.final_output == "no counts"
        assert completed.thread_usage is None
        observed = [
            event for event in await collect(handle.events()) if isinstance(event, UsageUpdated)
        ]
        assert observed and all(event.usage is None for event in observed)
        turns = [turn async for turn in session.turns()]
        assert len(turns) == 1
        assert turns[0]["usage"] is None
        assert len(responses.requests) == 1


async def test_native_restore_retains_total_and_separate_roots_do_not_share_usage(
    responses: ResponsesFixture,
) -> None:
    responses.before_event = _report_usage
    responses.respond(assistant_output("saved"))
    responses.respond(assistant_output("other root"))
    responses.respond(assistant_output("resumed"))
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    async with timeout(30):
        async with agent.session() as original:
            handle = await original.submit("remember this")
            saved_result = await (handle).get_result()
            assert saved_result.thread_usage is not None
            assert saved_result.thread_usage.total_token_usage == TokenUsage(17, 3, 4, 7, 2, 24)
            state = await original.save_state()
            saved_turns = [turn async for turn in original.turns()]
            assert saved_turns[0]["usage"] is not None
            assert saved_turns[0]["usage"]["input_tokens_details"]["cache_write_tokens"] == 4
            async with agent.session() as unrelated:
                other_handle = await unrelated.submit("independent")
                other = await (other_handle).get_result()
                assert unrelated.id != original.id
                assert other.thread_usage is not None
                assert other.thread_usage.total_token_usage == TokenUsage(17, 3, 4, 7, 2, 24)
        async with agent.session(state=state) as restored:
            assert [turn async for turn in restored.turns()] == saved_turns
            resumed_handle = await restored.submit("continue")
            resumed = await (resumed_handle).get_result()
            assert resumed.thread_usage is not None
            assert resumed.thread_usage.total_token_usage == TokenUsage(34, 6, 8, 14, 4, 48)
            assert resumed.thread_usage.last_token_usage == TokenUsage(17, 3, 4, 7, 2, 24)
            assert saved_result.thread_usage.total_token_usage == TokenUsage(17, 3, 4, 7, 2, 24)
        assert len(responses.requests) == 3
        assert "remember this" not in json.dumps(responses.requests[1]["input"])
        assert "remember this" in json.dumps(responses.requests[2]["input"])


@pytest.mark.parametrize("cache_write", [None, 0, 4])
def test_recorded_cache_writes_preserve_missing_values_and_state(cache_write: int | None) -> None:
    native_usage = {"input_tokens": 17, "cached_input_tokens": 3, "total_tokens": 24}
    if cache_write is not None:
        native_usage["cache_write_input_tokens"] = cache_write
    turns, _ = project_history(
        [
            {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
            {
                "type": "token_usage_record",
                "payload": {"turn_id": "turn", "turn_token_usage": native_usage},
            },
            {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn"}},
        ],
        "session",
    )
    usage = turns[0]["usage"]
    assert usage is not None
    expected_details = {"cached_tokens": 3}
    if cache_write is not None:
        expected_details["cache_write_tokens"] = cache_write
    assert usage["input_tokens_details"] == expected_details
    turns[0]["parent_session_id"] = "parent-session"
    turns[0]["parent_turn_id"] = "parent-turn"
    state = SessionState(
        format_version=2,
        codex_revision="synthetic-revision",
        source_session_id="session",
        history=[],
        root_metadata={},
        repair_turns=turns,
    )
    assert SessionState.from_json(state.to_json()).repair_turns == turns


async def test_cancel_joins_pending_native_usage_capture_before_admitting_next_turn(
    responses: ResponsesFixture,
) -> None:

    entered = asyncio.Event()
    release = asyncio.Event()
    responses.respond(assistant_output("first"))
    responses.respond(assistant_output("next"))
    agent = Agent(instructions="Answer.", provider=provider(responses))
    async with timeout(30), agent.session() as session:
        native_thread = session._native

        class DelayedRead:
            def __getattr__(self, name: str) -> Any:
                return getattr(native_thread, name)

            async def call(self, operation: str, *args: Any) -> Any:
                if operation == "token_usage_info" and not release.is_set():
                    entered.set()
                    await release.wait()
                return await native_thread.call(operation, *args)

        session._native = DelayedRead()
        cancelling = None
        try:
            first = await session.submit("first")
            await entered.wait()
            # Public start remains idle-only until usage capture has joined.
            with pytest.raises(RuntimeError, match="active submission"):
                await session.submit("must remain busy")
            assert len(responses.requests) == 1
            cancelling = asyncio.create_task(first.cancel())
            await asyncio.sleep(0)
            assert not cancelling.done()
            # The accepted stop must not admit or record a successor while
            # native usage capture remains unfinished.
            with pytest.raises(
                RuntimeError,
                match="finishing root interruption",
            ):
                await session.submit("still busy")
            assert len(responses.requests) == 1
            release.set()
            await cancelling
            with pytest.raises(TurnStoppedError):
                await (first).get_result()
            following = await session.submit("now idle")
            result = await (following).get_result()
            assert result.final_output == "next"
            assert result.thread_usage is not None
            assert result.thread_usage.total_token_usage == TokenUsage(22, 0, 0, 6, 0, 28)
            assert len(responses.requests) == 2
            recorded_input = json.dumps(responses.requests[-1]["input"])
            assert "must remain busy" not in recorded_input and "still busy" not in recorded_input
        finally:
            # Release the test-owned read before Session joins its native work.
            release.set()
            if cancelling is not None:
                await asyncio.gather(cancelling, return_exceptions=True)
