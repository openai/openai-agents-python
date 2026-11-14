"""Native remembered MCP choices remain scoped to one live declaration owner."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agents import Agent, ApprovalRequest, HttpMCP, PendingRequests
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import Peer, mcp_call


@pytest.mark.parametrize("approve", [False, True], ids=["deny", "allow"])
async def test_remembered_choice_reuses_exact_raw_tool_only(
    responses: ResponsesFixture, approve: bool
) -> None:
    peer = Peer(names=("write-one", "write-two"), annotations=None)
    async with timeout(30), peer.serve() as url:
        descriptor = HttpMCP(
            name="docs",
            url=url,
            tools=["write-one", "write-two"],
            tool_name_overrides={"write-one": "first", "write-two": "second"},
        )
        for name, call_id in [
            ("first", "original"),
            ("first", "remembered"),
            ("second", "other"),
            ("first", "after-other"),
            ("first", "fresh-session"),
        ]:
            responses.respond(mcp_call(name, "mcp__docs", call_id))
            responses.respond(assistant_output(call_id))
        agent = Agent(
            instructions="Use the chosen tool.",
            model="gpt-5.5",
            provider=provider(responses),
            tools=[descriptor],
        )
        async with agent.session() as session:
            first = await session.submit("first")
            pending = await first.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            request = pending.requests[0]
            assert isinstance(request, ApprovalRequest)
            await request.respond("allow_once" if approve else "deny", always=True)
            assert (await first.get_result()).final_output == "original"
            second = await session.submit("again")
            assert (await second.get_result()).final_output == "remembered"
            other = await session.submit("different raw tool")
            pending = await other.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            request = pending.requests[0]
            assert isinstance(request, ApprovalRequest)
            await request.respond("deny")
            assert (await other.get_result()).final_output == "other"
            fourth = await session.submit("original tool")
            assert (await fourth.get_result()).final_output == "after-other"
        async with agent.session() as session:
            fresh = await session.submit("new session")
            pending = await fresh.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            request = pending.requests[0]
            assert isinstance(request, ApprovalRequest)
            await request.respond("deny")
            assert (await fresh.get_result()).final_output == "fresh-session"
        assert [call["name"] for call in peer.calls] == ["write-one"] * (3 if approve else 0)
        assert all(call["arguments"] == {"key": "43"} for call in peer.calls)
        assert len(responses.requests) == 10
        assert "remembered_mcp" not in json.dumps(responses.requests)


@pytest.mark.parametrize("approve", [False, True], ids=["deny", "allow"])
async def test_remembered_stdio_choice_releases_its_peer(
    responses: ResponsesFixture, tmp_path, approve: bool
) -> None:
    from tests.test_native_mcp_stdio import assert_stopped, records, stdio

    log = tmp_path / "remembered-stdio.jsonl"
    descriptor = stdio(log, "--writable")
    for call_id in ("first", "second"):
        responses.respond(mcp_call("search", "mcp__local", call_id))
        responses.respond(assistant_output(call_id))
    async with (
        timeout(30),
        Agent(
            instructions="Use search.",
            model="gpt-5.5",
            provider=provider(responses),
            tools=[descriptor],
        ).session() as session,
    ):
        first = await session.submit("first")
        pending = await first.wait_for_request_or_result()
        assert isinstance(pending, PendingRequests)
        request = pending.requests[0]
        assert isinstance(request, ApprovalRequest)
        await request.respond("allow_once" if approve else "deny", always=True)
        assert (await first.get_result()).final_output == "first"
        second = await session.submit("second")
        assert (await second.get_result()).final_output == "second"
    calls = [
        row["message"]["params"]
        for row in records(log)
        if row.get("message", {}).get("method") == "tools/call"
    ]
    assert len(calls) == (2 if approve else 0)
    await assert_stopped(log)


async def test_remembered_choice_does_not_follow_changed_credentials(
    responses: ResponsesFixture,
) -> None:
    peer = Peer(names=("write",), annotations=None)
    async with timeout(30), peer.serve() as url:
        original = HttpMCP(
            name="docs", url=url, tools=["write"], bearer_token="old-synthetic-credential"
        )
        agent = Agent(
            instructions="Use write.",
            model="gpt-5.5",
            provider=provider(responses),
            tools=[original],
        )
        for call_id in ("original", "changed"):
            responses.respond(mcp_call("write", "mcp__docs", call_id))
            responses.respond(assistant_output(call_id))
        async with agent.session() as session:
            turn = await session.submit("original")
            pending = await turn.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            request = pending.requests[0]
            assert isinstance(request, ApprovalRequest)
            await request.respond("allow_once", always=True)
            assert (await turn.get_result()).final_output == "original"
            await session.update(
                tools=[replace(original, bearer_token="current-synthetic-credential")]
            )
            turn = await session.submit("changed")
            pending = await turn.wait_for_request_or_result()
            assert isinstance(pending, PendingRequests)
            request = pending.requests[0]
            assert isinstance(request, ApprovalRequest)
            await request.respond("deny")
            assert (await turn.get_result()).final_output == "changed"
        assert [call["_meta"]["callId"] for call in peer.calls] == ["original"]
        assert "Bearer current-synthetic-credential" in peer.auth
