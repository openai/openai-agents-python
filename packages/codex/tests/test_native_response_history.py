"""Response terminal observations preserve provider facts through native HTTP."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agents import Agent, NativeError, TurnStoppedError, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_agent import provider


@pytest.mark.parametrize("reason", ["max_output_tokens", "content_filter"])
async def test_incomplete_terminal_retains_facts_without_retry_or_tool_effect(
    responses: ResponsesFixture, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agents import _native

    create_engine = _native.create_engine

    def no_retries(config_json: str, *args: Any, **kwargs: Any) -> Any:
        config = json.loads(config_json)
        # Existing native policy retries incomplete errors. Select zero retries
        # for this terminal-facts test without changing the SDK's retry owner.
        config["model_providers.native_agent"]["stream_max_retries"] = 0
        return create_engine(json.dumps(config), *args, **kwargs)

    monkeypatch.setattr(_native, "create_engine", no_retries)
    effects: list[str] = []

    @function_tool
    def unexpected() -> str:
        effects.append("executed")
        return "unexpected"

    async def incomplete(event: dict[str, Any]) -> None:
        if event["type"] == "response.completed":
            event["type"] = "response.incomplete"
            event["response"]["incomplete_details"] = {"reason": reason}
            event["response"]["usage"] = {
                "input_tokens": 2**60 + 1,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 0,
                "output_tokens_details": {},
            }

    responses.before_event = incomplete
    partial = function_call("unexpected", {}, "never-execute")
    partial["type"] = "response.output_item.added"
    partial["item"]["arguments"] = "{"
    responses.respond(partial)
    async with (
        timeout(30),
        Agent(
            instructions="Answer.", tools=[unexpected], provider=provider(responses)
        ).session() as session,
    ):
        handle = await session.submit("synthetic incomplete response")
        with pytest.raises(NativeError):
            await handle.get_result()
        turns = [turn async for turn in session.turns()]
        assert turns[0]["status"] == "failed"
        assert turns[0]["usage"] is None
        observed = turns[0]["responses"]
        assert len(observed) == 1
        assert observed[0]["response_id"] == "resp_1"
        assert observed[0]["status"] == "incomplete"
        assert observed[0]["incomplete_reason"] == reason
        assert observed[0]["usage"] == {
            "input_tokens": 2**60 + 1,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 0,
        }
        assert effects == []
        assert len(responses.requests) == 1


async def test_provider_completed_remains_visible_when_sdk_rejects_completion(
    responses: ResponsesFixture,
) -> None:
    responses.respond()  # No supported output: the SDK integrity guard rejects Completed.
    async with (
        timeout(30),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        handle = await session.submit("synthetic empty completed terminal")
        with pytest.raises(NativeError, match="without complete supported output"):
            await handle.get_result()
        turns = [turn async for turn in session.turns()]
        # Early SDK failure may interrupt before Core delivers its error.
        assert turns[0]["status"] in {"failed", "cancelled"}
        assert turns[0]["responses"][0]["status"] == "completed"
        assert turns[0]["responses"][0]["usage_source"] == "received"
        assert len(responses.requests) == 1


@pytest.mark.parametrize("reported", [None, {}, {"cached_tokens": 0, "cache_write_tokens": 0}])
async def test_missing_received_usage_survives_save_restore_without_reminting(
    responses: ResponsesFixture, reported: dict[str, int] | None
) -> None:
    async def modify(event: dict[str, Any]) -> None:
        if event["type"] == "response.completed":
            event["response"]["usage"] = (
                None
                if reported is None
                else {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    **({"input_tokens_details": reported} if reported else {}),
                }
            )

    responses.before_event = modify
    responses.respond(assistant_output("saved"))
    agent = Agent(instructions="Answer.", provider=provider(responses))
    async with timeout(30):
        async with agent.session() as session:
            handle = await session.submit("remember")
            assert (await handle.get_result()).final_output == "saved"
            turns = [turn async for turn in session.turns()]
            response = turns[0]["responses"][0]
            expected = (
                None
                if reported is None
                else {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    **({"input_tokens_details": reported} if reported else {}),
                }
            )
            assert response["usage"] == expected
            assert response["id"] is not None
            assert response["thread_id"] == session.id
            state = await session.save_state()
            assert [turn async for turn in session.turns()] == turns
        async with agent.session(state=state) as restored:
            assert [turn async for turn in restored.turns()] == turns
            assert [turn async for turn in restored.turns()] == turns
        assert len(responses.requests) == 1


async def test_interrupted_terminal_keeps_incomplete_status_and_existing_continuation(
    responses: ResponsesFixture,
) -> None:
    async def interrupted(event: dict[str, Any]) -> None:
        if event["type"] == "response.completed" and event["response"]["id"] == "resp_1":
            event["type"] = "response.incomplete"
            event["response"]["incomplete_details"] = {"reason": "interrupted"}

    responses.before_event = interrupted
    responses.respond(assistant_output("partial"))
    responses.respond(assistant_output("finished"))
    async with (
        timeout(30),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        handle = await session.submit("continue the interrupted response")
        assert (await handle.get_result()).final_output == "finished"
        turns = [turn async for turn in session.turns()]
        observed = turns[0]["responses"]
        assert [response["response_id"] for response in observed] == ["resp_1", "resp_2"]
        assert [response.get("status") for response in observed] == ["incomplete", "completed"]
        assert observed[0]["incomplete_reason"] == "interrupted"
        assert len(responses.requests) == 2


async def test_failed_provider_terminal_retains_only_safe_typed_facts(
    responses: ResponsesFixture,
) -> None:
    responses.respond(
        {
            "type": "response.failed",
            "response": {
                "id": "resp_failed",
                "usage": {"input_tokens": 9},
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_prompt",
                    "message": "synthetic private provider detail",
                },
            },
        }
    )
    async with (
        timeout(30),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        handle = await session.submit("synthetic failed terminal")
        with pytest.raises(NativeError):
            await handle.get_result()
        turns = [turn async for turn in session.turns()]
        observed = turns[0]["responses"]
        assert len(observed) == 1
        assert observed[0]["response_id"] == "resp_failed"
        assert observed[0]["status"] == "failed"
        assert observed[0]["usage"] == {"input_tokens": 9}
        assert "synthetic private provider detail" not in repr(observed)
        assert len(responses.requests) == 1


async def test_cancellation_before_terminal_does_not_invent_response_facts(
    responses: ResponsesFixture,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_terminal(event: dict[str, Any]) -> None:
        if event["type"] == "response.completed":
            entered.set()
            await release.wait()

    responses.before_event = hold_terminal
    responses.respond(assistant_output("unsettled"))
    async with (
        timeout(30),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        handle = await session.submit("cancel before the received terminal")
        try:
            await entered.wait()
            await handle.cancel()
            with pytest.raises(TurnStoppedError):
                await handle.get_result()
            turns = [turn async for turn in session.turns()]
            assert turns[0]["status"] == "cancelled"
            assert "responses" not in turns[0]
            assert len(responses.requests) == 1
        finally:
            release.set()


async def test_fork_compaction_and_restore_preserve_response_occurrences(
    responses: ResponsesFixture,
) -> None:
    responses.respond(assistant_output("remembered"))
    responses.respond(assistant_output("summary of remembered context"))
    agent = Agent(instructions="Answer.", provider=provider(responses))
    async with timeout(30):
        async with agent.session() as source:
            handle = await source.submit("remember this context")
            await handle.get_result()
            first_turns = [turn async for turn in source.turns()]
            first_response = first_turns[0]["responses"][0]
            assert first_response["thread_id"] == source.id
            forked_state = await source.fork()
            await source.compact()
            compacted_turns = [turn async for turn in source.turns()]
            retained = [
                response for turn in compacted_turns for response in turn.get("responses", [])
            ]
            assert len(retained) == 2
            assert retained[0] == first_response
            assert len({response["id"] for response in retained}) == 2
            assert [response["response_id"] for response in retained] == ["resp_1", "resp_2"]
            compacted_state = await source.save_state()
            assert any(record["type"] == "compacted" for record in compacted_state.history)
        async with agent.session(state=forked_state) as forked:
            inherited = [turn async for turn in forked.turns()]
            assert forked.id != first_response["thread_id"]
            assert inherited[0]["responses"] == [first_response]
            assert inherited[0]["usage"] == first_turns[0]["usage"]
            assert len(responses.requests) == 2
        async with agent.session(state=compacted_state) as restored:
            assert [turn async for turn in restored.turns()] == compacted_turns
            assert [turn async for turn in restored.turns()] == compacted_turns
            assert len(responses.requests) == 2


async def test_existing_native_retry_retains_failed_and_recovered_response_facts(
    responses: ResponsesFixture,
) -> None:
    async def incomplete_once(event: dict[str, Any]) -> None:
        if event["type"] == "response.completed" and event["response"]["id"] == "resp_1":
            event["type"] = "response.incomplete"
            event["response"]["incomplete_details"] = {"reason": "max_output_tokens"}

    responses.before_event = incomplete_once
    responses.respond(assistant_output("partial"))
    responses.respond(assistant_output("recovered"))
    async with (
        timeout(30),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        handle = await session.submit("recover from one incomplete response")
        completed = await handle.get_result()
        assert completed.final_output == "recovered"
        assert completed.thread_usage is not None
        assert completed.thread_usage.total_token_usage.total_tokens == 14
        assert completed.thread_usage.last_token_usage.total_tokens == 14
        turns = [turn async for turn in session.turns()]
        assert turns[0]["status"] == "completed"
        observed = turns[0]["responses"]
        assert [response["response_id"] for response in observed] == ["resp_1", "resp_2"]
        assert [response.get("status") for response in observed] == ["incomplete", "completed"]
        assert len({response["id"] for response in observed}) == 2
        assert observed[0]["incomplete_reason"] == "max_output_tokens"
        assert all(response["usage"] is not None for response in observed)
        assert len(responses.requests) == 2


async def test_websocket_terminal_keeps_received_unknown_and_zero_counters() -> None:
    from tests.test_native_provider_websockets import peer

    async with timeout(30), peer() as websocket:
        websocket.completion_id_override = "ws-terminal"
        websocket.respond(assistant_output("websocket answer"))

        async def counters(event: dict[str, Any]) -> None:
            if event["type"] == "response.completed":
                event["response"]["usage"] = {
                    "input_tokens": 0,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens": 3,
                    "total_tokens": 3,
                }

        websocket.before_event = counters
        agent = Agent(instructions="Answer.", model="gpt-5.2", provider=websocket.provider())
        async with agent.session() as session:
            handle = await session.submit("use the websocket transport")
            result = await handle.get_result()
            assert result.final_output == "websocket answer"
            assert result.thread_usage is not None
            assert result.thread_usage.total_token_usage.total_tokens == 3
            turns = [turn async for turn in session.turns()]
            assert len(turns[0]["responses"]) == 1
            observed = turns[0]["responses"][0]
            assert observed["response_id"] == "ws-terminal"
            assert observed["status"] == "completed"
            assert observed["usage_source"] == "received"
            assert observed["usage"] == {
                "input_tokens": 0,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 3,
                "total_tokens": 3,
            }
            assert websocket.connections == 1
            assert len(websocket.requests) == 1
            assert websocket.http_requests == []
