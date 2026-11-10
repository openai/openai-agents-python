"""Public native UI streams preserve identity, privacy and passive ownership."""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

import pytest

from agents import Agent, HttpMCP, function_tool
from agents.ui import vercel_stream
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    function_call,
    responses as responses,
    text_response,
)
from tests.test_native_agent import provider
from tests.test_native_mcp_configuration_values import _SECRET, assert_private_frames
from tests.test_native_mcp_tools import Peer


def frame(value: bytes) -> Any:
    payload = value.decode()[6:-2]
    return payload if payload == "[DONE]" else json.loads(payload)


@pytest.mark.parametrize("include_tool_data", [False, True])
async def test_public_native_text_tool_and_terminal(
    responses: ResponsesFixture, include_tool_data: bool
) -> None:
    @function_tool
    async def lookup(key: str) -> str:
        """Read a record."""
        return "result:" + key

    responses.respond(function_call("lookup", {"key": "earlier-secret"}, "old-call"))
    responses.respond(*text_response("old-message", "old answer"))
    responses.respond(function_call("lookup", {"key": "current-secret"}, "current-call"))
    responses.respond(*text_response("current-message", "Found ", "it."))
    async with (
        timeout(30),
        Agent(
            instructions="Read.", tools=[lookup], provider=provider(responses)
        ).session() as session,
    ):
        await (await session.submit("earlier")).get_result()
        turn = await session.submit("current")
        frames = [
            frame(value)
            async for value in vercel_stream(
                turn, session=session, include_tool_data=include_tool_data
            )
        ]
        assert (await turn.get_result()).final_output == "Found it."
        assert frames[-2:] == [{"type": "finish"}, "[DONE]"]
        assert [
            f["delta"] for f in frames if isinstance(f, dict) and f["type"] == "text-delta"
        ] == ["Found ", "it."]
        wire = json.dumps(frames)
        assert "earlier-secret" not in wire and "old-call" not in wire
        if include_tool_data:
            assert {
                "type": "tool-input-available",
                "toolCallId": "current-call",
                "toolName": "lookup",
                "input": {"key": "current-secret"},
                "dynamic": True,
            } in frames
            outputs = [
                f for f in frames if isinstance(f, dict) and f["type"] == "tool-output-available"
            ]
            assert len(outputs) == 1 and outputs[0]["toolCallId"] == "current-call"
            assert "result:current-secret" in json.dumps(outputs[0]["output"])
        else:
            assert "current-secret" not in wire
            assert not any(isinstance(f, dict) and f["type"].startswith("tool-") for f in frames)


async def test_close_public_reader_keeps_native_turn_usable(responses: ResponsesFixture) -> None:
    responses.respond(*text_response("message", "still running"))
    async with (
        timeout(30),
        Agent(instructions="Read.", provider=provider(responses)).session() as session,
    ):
        turn = await session.submit("read")
        body = vercel_stream(turn, session=session)
        assert frame(await anext(body)) == {"type": "start"}
        await body.aclose()
        assert (await turn.get_result()).final_output == "still running"


@pytest.mark.parametrize("keyword_turn", [False, True])
@pytest.mark.parametrize("owners", ["same", "foreign_session", "foreign_turn", "neither"])
async def test_ui_validation_protects_both_native_owners(
    responses: ResponsesFixture, owners: str, keyword_turn: bool
) -> None:
    responses.respond(*text_response("message", "done"))
    async with timeout(30), Peer().serve() as url:
        declaration = HttpMCP(
            name="private", url=url, tools=["search"], headers={"X-Token": _SECRET}
        )
        async with (
            Agent(
                instructions="Read.", tools=[declaration], provider=provider(responses)
            ).session() as protected,
            Agent(instructions="Read.", provider=provider(responses)).session() as plain,
        ):
            owner = plain if owners in {"foreign_session", "neither"} else protected
            supplied = plain if owners in {"foreign_turn", "neither"} else protected
            turn = await owner.submit("read")
            assert (await turn.get_result()).final_output == "done"
            options: dict[str, Any] = {"session": supplied}
            if owners in {"same", "neither"}:
                options["include_tool_data"] = 1
            body = (
                vercel_stream(turn=turn, **options)
                if keyword_turn
                else vercel_stream(turn, **options)
            )
            with pytest.raises(
                TypeError if owners in {"same", "neither"} else ValueError,
                match="include_tool_data must be a bool"
                if owners in {"same", "neither"}
                else "owning Session",
            ) as caught:
                await anext(body)
            if owners == "neither":
                traceback = caught.value.__traceback__
                assert any(
                    entry.tb_frame.f_globals.get("__name__") == "_agents_codex.ui"
                    for entry in _tracebacks(traceback)
                )
            else:
                assert_private_frames(caught.value, application_files=(__file__,))
            await body.aclose()


def _tracebacks(traceback):
    while traceback is not None:
        yield traceback
        traceback = traceback.tb_next


@pytest.mark.parametrize("has_mcp", [False, True])
async def test_cancelled_ui_reader_keeps_credentials_private_and_turn_live(
    responses: ResponsesFixture, has_mcp: bool
) -> None:
    responses.respond(*text_response("message", "still running"))
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(event: Any) -> None:
        entered.set()
        await release.wait()

    responses.before_event = hold
    async with timeout(30), Peer().serve() as url:
        tools = (
            [HttpMCP(name="private", url=url, tools=["search"], headers={"X-Token": _SECRET})]
            if has_mcp
            else []
        )
        async with Agent(
            instructions="Read.", tools=tools, provider=provider(responses)
        ).session() as session:
            turn = await session.submit("read")
            body = vercel_stream(turn, session=session)
            assert frame(await anext(body)) == {"type": "start"}
            reading = asyncio.create_task(anext(body))
            try:
                await entered.wait()
                await asyncio.sleep(0)
                assert not reading.done()
                reading.cancel()
                with pytest.raises(asyncio.CancelledError) as caught:
                    await reading
                if has_mcp:
                    assert_private_frames(
                        caught.value, allow_chain=True, application_files=(__file__,)
                    )
            finally:
                release.set()
            await body.aclose()
            assert (await turn.get_result()).final_output == "still running"


@pytest.mark.parametrize("has_mcp", [False, True])
async def test_ui_async_generator_protocol_preserves_error_frames(
    responses: ResponsesFixture, has_mcp: bool
) -> None:
    responses.respond(*text_response("message", "done"))
    async with timeout(30), Peer().serve() as url:
        tools = (
            [HttpMCP(name="private", url=url, tools=["search"], headers={"X-Token": _SECRET})]
            if has_mcp
            else []
        )
        async with Agent(
            instructions="Read.", tools=tools, provider=provider(responses)
        ).session() as session:
            turn = await session.submit("read")
            assert inspect.isasyncgenfunction(vercel_stream)
            body = vercel_stream(turn, session=session)
            assert frame(await body.asend(None)) == {"type": "start"}
            assert frame(await body.athrow(ValueError("Synthetic client interruption"))) == {
                "type": "error",
                "errorText": "turn_failed",
            }
            assert frame(await body.asend(None)) == "[DONE]"
            with pytest.raises(StopAsyncIteration):
                await anext(body)
            await body.aclose()
            assert (await turn.get_result()).final_output == "done"


async def test_done_only_response_requires_resync(responses: ResponsesFixture) -> None:
    from tests.native_fixtures import assistant_output

    responses.respond(assistant_output("no delta"))
    async with (
        timeout(30),
        Agent(instructions="Read.", provider=provider(responses)).session() as session,
    ):
        turn = await session.submit("read")
        frames = [frame(value) async for value in vercel_stream(turn, session=session)]
        assert frames[-2:] == [{"type": "error", "errorText": "resync_required"}, "[DONE]"]
        assert (await turn.get_result()).final_output == "no delta"


@pytest.mark.parametrize(
    "commentary", ["retained commentary", "", "x" * 1_048_577], ids=["text", "empty", "over-bound"]
)
@pytest.mark.parametrize("final_chunks", [("Final answer.",), ()])
async def test_done_only_commentary_is_checked_before_finish(
    responses: ResponsesFixture, commentary: str, final_chunks: tuple[str, ...]
) -> None:
    from tests.native_fixtures import assistant_output

    retained = assistant_output(commentary)
    retained["item"]["phase"] = "commentary"
    final_events = text_response("final-message", *final_chunks)
    for event in final_events:
        if "item" in event:
            event["item"]["phase"] = "final_answer"
    responses.respond(retained, *final_events)
    async with (
        timeout(30),
        Agent(instructions="Read.", provider=provider(responses)).session() as session,
    ):
        turn = await session.submit("read")
        frames = [frame(value) async for value in vercel_stream(turn, session=session)]
        assert frames[-2:] == [
            {"type": "error", "errorText": "resync_required"} if commentary else {"type": "finish"},
            "[DONE]",
        ]
        assert (await turn.get_result()).final_output == "".join(final_chunks)
        assert [
            value["delta"]
            for value in frames
            if isinstance(value, dict) and value["type"] == "text-delta"
        ] == list(final_chunks)


async def test_public_native_mcp_payload_uses_canonical_call_id(
    responses: ResponsesFixture,
) -> None:
    from agents import HttpMCP
    from tests.test_native_mcp_tools import Peer, mcp_call

    peer = Peer(names=("search",))
    responses.respond(mcp_call("search", "mcp__docs", "wire-call"))
    responses.respond(*text_response("mcp-final", "Found record."))
    async with timeout(30), peer.serve() as url:
        agent = Agent(
            instructions="Search.",
            tools=[HttpMCP(name="docs", url=url, tools=["search"])],
            provider=provider(responses),
        )
        async with agent.session() as session:
            turn = await session.submit("Search")
            frames = [
                frame(value)
                async for value in vercel_stream(turn, session=session, include_tool_data=True)
            ]
            recorded = [item async for item in session.items() if item["type"] == "mcp_call"]
            assert len(recorded) == 1
            call_id = recorded[0]["call_id"]
            assert {
                "type": "tool-input-available",
                "toolCallId": call_id,
                "toolName": "docs.search",
                "input": {"key": "43"},
                "dynamic": True,
            } in frames, frames
            assert {
                "type": "tool-output-available",
                "toolCallId": call_id,
                "output": recorded[0]["output"],
                "dynamic": True,
            } in frames
            assert frames[-2:] == [{"type": "finish"}, "[DONE]"]


async def test_native_observation_gap_never_claims_success(responses: ResponsesFixture) -> None:
    responses.respond(*text_response("overflowed-events", *(["x"] * 350)))
    async with (
        timeout(30),
        Agent(instructions="Read.", provider=provider(responses)).session() as session,
    ):
        turn = await session.submit("read")
        assert (await turn.get_result()).final_output == "x" * 350
        frames = [frame(value) async for value in vercel_stream(turn, session=session)]
        assert frames == [
            {"type": "start"},
            {"type": "error", "errorText": "resync_required"},
            "[DONE]",
        ]


async def test_native_text_projection_bound_preserves_turn(responses: ResponsesFixture) -> None:
    text = "x" * 1_048_577
    responses.respond(*text_response("large-text", text))
    async with (
        timeout(30),
        Agent(instructions="Read.", provider=provider(responses)).session() as session,
    ):
        turn = await session.submit("read")
        frames = [frame(value) async for value in vercel_stream(turn, session=session)]
        assert frames[-2:] == [{"type": "error", "errorText": "resync_required"}, "[DONE]"]
        assert not any(
            isinstance(value, dict) and value["type"] == "text-delta" for value in frames
        )
        assert (await turn.get_result()).final_output == text
