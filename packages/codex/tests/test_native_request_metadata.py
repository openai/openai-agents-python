"""Installed native public-route qualification for passive root request metadata."""

from __future__ import annotations

from dataclasses import fields
from typing import Any

import pytest

from agents import Agent, NativeError, function_tool
from agents.observation import ModelRequestDispatching, ModelResponseLinked
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_provider_websockets import peer


@pytest.mark.parametrize("mode", ["http", "websocket", "fallback"])
async def test_public_request_fields_and_exact_links_with_repeated_provider_ids(mode: str) -> None:
    @function_tool
    def probe() -> str:
        return "synthetic-result"

    async with timeout(30), peer() as server:
        if mode == "fallback":
            server.upgrade_status = 426
        server.completion_id_override = "same-provider-response"

        async def repeated(event: dict[str, Any]) -> None:
            if event["type"] == "response.completed":
                event["response"]["id"] = "same-provider-response"

        server.before_event = repeated
        server.respond(function_call("probe", {}, "call-one"))
        server.respond(assistant_output("done"))
        async with Agent(
            instructions="Use probe then answer.",
            tools=[probe],
            provider=server.provider(websocket=mode != "http"),
            model_settings={
                "reasoning_effort": "low",
                "reasoning_summary": "concise",
                "service_tier": "flex",
            },
        ).session() as session:
            turn = await session.submit("PRIVATE_INPUT_MARKER")
            assert (await turn.get_result()).final_output == "done"
            observed = [event async for event in turn.events()]
            dispatches = [event for event in observed if isinstance(event, ModelRequestDispatching)]
            links = [event for event in observed if isinstance(event, ModelResponseLinked)]
            assert len(dispatches) == len(server.requests) == 2
            assert len({event.request_id for event in dispatches}) == 2
            for event, request in zip(dispatches, server.requests, strict=True):
                assert event.submission_id == turn.submission_id
                assert event.thread_id == session.id and event.turn_id == turn.turn_id
                assert event.model == request["model"]
                assert event.reasoning_effort == request.get("reasoning", {}).get("effort")
                assert event.reasoning_summary == request.get("reasoning", {}).get("summary")
                assert event.service_tier == request.get("service_tier")
                assert event.transport == ("websocket" if mode == "websocket" else "http_sse")
                assert "PRIVATE_INPUT_MARKER" not in repr(event)
                assert {item.name for item in fields(event)}.isdisjoint(
                    {"body", "headers", "url", "input"}
                )
            recorded = [row async for row in session.turns()]
            responses = recorded[0].get("responses")
            assert responses is not None
            assert len(responses) == len(links) == 2
            assert {row["response_id"] for row in responses} == {"same-provider-response"}
            assert {event.response_observation_id for event in links} == {
                row["id"] for row in responses
            }
            assert {event.request_id for event in links} == {
                event.request_id for event in dispatches
            }
            for dispatch, link, response in zip(dispatches, links, responses, strict=True):
                assert link.request_id == dispatch.request_id
                assert link.session_id == dispatch.session_id == session.id
                assert link.thread_id == dispatch.thread_id
                assert link.turn_id == dispatch.turn_id
                assert link.submission_id == dispatch.submission_id
                assert link.response_observation_id == response["id"]
            assert [event async for event in turn.events()] == observed
            if mode == "fallback":
                assert server.upgrades and not server.frames


@pytest.mark.parametrize("websocket", [False, True])
async def test_failed_without_completion_retains_metadata_without_inventing_link(
    websocket: bool,
) -> None:
    async with timeout(30), peer() as server:
        invalid = {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "refusal", "refusal": "PRIVATE_REFUSAL"}],
            },
        }
        server.expected_disconnect_event = invalid
        server.respond(invalid)
        async with Agent(
            instructions="Answer.", provider=server.provider(websocket=websocket)
        ).session() as session:
            failed = await session.submit("go")
            with pytest.raises(NativeError):
                await failed.get_result()
            events = [event async for event in failed.events()]
            assert (
                len([event for event in events if isinstance(event, ModelRequestDispatching)]) == 1
            )
            assert not any(isinstance(event, ModelResponseLinked) for event in events)
            assert "PRIVATE_REFUSAL" not in repr(events)
            assert len(server.requests) == 1


@pytest.mark.parametrize("websocket", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_reader_closure_is_passive_and_owner_cancellation_keeps_dispatch(
    websocket: bool, cancel: bool
) -> None:
    import asyncio

    from agents import TurnStoppedError

    entered = asyncio.Event()
    release = asyncio.Event()
    async with timeout(30), peer() as server:
        output = assistant_output("done")
        server.expected_disconnect_event = output

        async def hold(event: dict[str, Any]) -> None:
            if event["type"] == "response.output_item.done":
                entered.set()
                await release.wait()

        server.before_event = hold
        server.respond(output)
        async with Agent(
            instructions="Answer.", provider=server.provider(websocket=websocket)
        ).session() as session:
            turn = await session.submit("go")
            reader = turn.events()
            first = None
            async for event in reader:
                if isinstance(event, ModelRequestDispatching):
                    first = event
                    break
            assert first is not None
            await reader.aclose()
            await entered.wait()
            if cancel:
                await turn.cancel()
                with pytest.raises(TurnStoppedError):
                    await turn.get_result()
            release.set()
            if not cancel:
                assert (await turn.get_result()).final_output == "done"
            events = [event async for event in turn.events()]
            assert first in events
            assert len(server.requests) == 1
            if cancel:
                assert not any(isinstance(event, ModelResponseLinked) for event in events)


async def test_prompted_correction_preserves_its_own_request_owner_and_parent_link() -> None:
    from typing import Literal

    from pydantic import BaseModel

    from agents import PromptedOutput

    class Answer(BaseModel):
        answer: Literal["done"]

    async with timeout(30), peer() as server:
        server.respond(assistant_output("not valid JSON"))
        server.respond(assistant_output('{"answer":"done"}'))
        async with Agent(
            instructions="Answer.",
            provider=server.provider(websocket=False),
            output_type=PromptedOutput(Answer, max_retries=1),
        ).session() as session:
            turn = await session.submit("go")
            assert (await turn.get_result()).final_output == Answer(answer="done")
            events = [event async for event in turn.events()]
            dispatches = [event for event in events if isinstance(event, ModelRequestDispatching)]
            assert len(dispatches) == len(server.requests) == 2
            original, correction = dispatches
            assert original.thread_id == original.session_id == session.id
            assert original.submission_id == turn.submission_id
            assert correction.thread_id == correction.session_id != session.id
            assert correction.submission_id != turn.submission_id
            assert correction.parent_submission_id == turn.submission_id
            assert correction.request_id != original.request_id
            for event, request in zip(dispatches, server.requests, strict=True):
                assert event.model == request["model"]
                assert event.reasoning_effort == request.get("reasoning", {}).get("effort")
            links = [event for event in events if isinstance(event, ModelResponseLinked)]
            assert len(links) == 2
            for event in links:
                owner = next(row for row in dispatches if row.request_id == event.request_id)
                assert (event.thread_id, event.submission_id, event.parent_submission_id) == (
                    owner.thread_id,
                    owner.submission_id,
                    owner.parent_submission_id,
                )


@pytest.mark.parametrize("restore", [False, True])
async def test_manual_followup_and_restored_followup_bind_current_ephemeral_owner(
    restore: bool,
) -> None:
    from contextlib import AsyncExitStack

    from agents import FunctionTool
    from agents.state import SessionState
    from tests.test_native_manual_tools import LookupArgs, next_request

    lookup = FunctionTool(
        name="lookup",
        description="Read a synthetic record.",
        input_type=LookupArgs,
        output_type=str,
    )
    async with timeout(30), peer() as server, AsyncExitStack() as stack:
        agent = Agent(
            instructions="Use lookup.",
            tools=[lookup],
            provider=server.provider(websocket=False),
        )
        server.respond(function_call("lookup", {"key": "A"}, "manual-A"))
        source = await stack.enter_async_context(agent.session())
        original = await source.submit("read A")
        action = await next_request(source.interactions(), lookup)
        original_rows = [row async for row in source.turns()]
        prior_ids = {
            response["id"] for row in original_rows for response in row.get("responses", [])
        }
        assert prior_ids
        owner = source
        handle = original
        if restore:
            saved = await source.suspend()
            encoded = saved.to_json()
            assert "observation_submission_id" not in encoded
            assert original.submission_id not in encoded
            owner = await stack.enter_async_context(
                agent.session(state=SessionState.from_json(encoded))
            )
            restored = owner.restored_turn
            assert restored is not None and restored.submission_id != original.submission_id
            handle = restored
            action = await next_request(owner.interactions(), lookup)
        assert len(server.requests) == 1
        server.respond(assistant_output("continued"))
        await action.respond("record A")
        assert (await handle.get_result()).final_output == "continued"
        events = [event async for event in handle.events()]
        dispatches = [event for event in events if isinstance(event, ModelRequestDispatching)]
        links = [event for event in events if isinstance(event, ModelResponseLinked)]
        assert len(dispatches) == len(links) == (1 if restore else 2)
        assert all(
            event.submission_id == handle.submission_id and event.thread_id == owner.id
            for event in dispatches
        )
        assert dispatches[-1].turn_id == original.turn_id
        if restore:
            assert all(event.response_observation_id not in prior_ids for event in links)
        current_rows = [row async for row in owner.turns()]
        retained_ids = {
            response["id"] for row in current_rows for response in row.get("responses", [])
        }
        assert prior_ids <= retained_ids
