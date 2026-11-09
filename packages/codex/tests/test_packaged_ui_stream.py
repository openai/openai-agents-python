"""Public native UI streams preserve identity, privacy and passive ownership."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agents import Agent, function_tool
from agents.ui import vercel_stream
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    function_call,
    responses as responses,
    text_response,
)
from tests.test_native_agent import provider


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
