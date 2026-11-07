"""Selected MCP approval stays native and independent of peer annotations."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from typing import Any, cast

import pytest

from agents import Agent, ApprovalRequest, HttpMCP, MCPResultContext, NativeError
from agents.observation import ToolFinished, ToolStarted
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_agent import provider
from tests.test_native_agent_observation import collect
from tests.test_native_mcp_stdio import assert_stopped, records, stdio
from tests.test_native_mcp_tools import Peer, mcp_call


@pytest.mark.parametrize("mode", ["auto", "approve", "prompt", "always", "", None, True, {}])
def test_approval_mode_rejects_unsupported_values_before_io(mode: object) -> None:
    with pytest.raises((TypeError, ValueError), match="approval_mode"):
        HttpMCP(
            name="docs",
            url="https://example.invalid/mcp",
            tools=["search"],
            approval_mode=cast(Any, mode),
        )


def test_approval_capture_is_frozen_and_covers_only_selected_raw_names() -> None:
    names = ["find-record"]
    descriptor = HttpMCP(
        name="docs", url="https://example.invalid/mcp", tools=names, approval_mode="preapproved"
    )
    names.append("excluded")
    assert descriptor.tools == ("find-record",)
    assert descriptor._config()["tools"] == {"find-record": {"approval_mode": "approve"}}
    with pytest.raises(FrozenInstanceError):
        descriptor.approval_mode = "native"
    default = replace(descriptor, approval_mode="native")
    assert "tools" not in default._config()
    assert default._config()["enabled_tools"] == ["find-record"]


@pytest.mark.parametrize("transport", ["http", "stdio"])
@pytest.mark.parametrize("approved", [False, True], ids=["native", "preapproved"])
async def test_nonreadonly_peer_uses_native_approval_and_exact_effect(
    responses: ResponsesFixture, tmp_path: Path, transport: str, approved: bool
) -> None:
    peer = Peer(names=("find-record", "excluded"), annotations=None)
    log = tmp_path / "stdio.jsonl"
    async with timeout(30), AsyncExitStack() as stack:
        if transport == "http":
            url = await stack.enter_async_context(peer.serve())
            descriptor = HttpMCP(name="docs", url=url, tools=["find-record"])
            name = "find_record"
        else:
            descriptor = replace(stdio(log, "--writable"), name="docs")
            name = "search"
        if approved:
            descriptor = replace(descriptor, approval_mode="preapproved")
        responses.respond(mcp_call(name, "mcp__docs", "write-call"))
        responses.respond(assistant_output("done"))
        async with Agent(
            instructions="Use the selected tool.",
            model="gpt-5.5",
            provider=provider(responses),
            tools=[descriptor],
        ).session() as session:
            handle = await session.submit("write")
            if not approved:
                cursor = session.interactions()
                request = await anext(cursor)
                assert isinstance(request, ApprovalRequest)
                from agents import PendingRequests

                batch = await handle.wait_for_request_or_result()
                assert isinstance(batch, PendingRequests) and batch.turn is handle
                assert batch.requests == (request,)
                await request.respond("deny")
                await cursor.aclose()
            assert (await handle.get_result()).final_output == "done"
            history = [item async for item in session.items()]
            events = await collect(handle.events())
            assert await collect(handle.events()) == events
            tools = [event for event in events if isinstance(event, (ToolStarted, ToolFinished))]
            assert tools and tools[0].type == "tool_started"
            assert all(
                event.family == "mcp"
                and event.turn_id == handle.turn_id
                and event.call_id == "write-call"
                for event in tools
            )
            assert all(event.namespace == "mcp__docs" and event.name == name for event in tools)
            if approved:
                assert [event.type for event in tools] == ["tool_started", "tool_finished"]
                assert isinstance(tools[-1], ToolFinished) and tools[-1].status == "completed"
                outputs = [
                    item
                    for item in history
                    if item.get("type") == "function_call_output"
                    and item.get("call_id") == "write-call"
                ]
                assert len(outputs) == 1
                assert (
                    "native-receipt" if transport == "http" else "private-stdio-result"
                ) in json.dumps(outputs)
                assert len(responses.requests) == 2
            else:
                assert len(responses.requests) == 2
            assert all('"key": "43"' not in json.dumps(asdict(event)) for event in tools)
        calls = (
            peer.calls
            if transport == "http"
            else [
                row["message"]["params"]
                for row in records(log)
                if row.get("message", {}).get("method") == "tools/call"
            ]
        )
        assert len(calls) == int(approved)
        if calls:
            assert calls[0]["_meta"]["callId"] == "write-call"
            assert calls[0]["name"] == ("find-record" if transport == "http" else "search")
            assert calls[0]["arguments"] == {"key": "43"}
        if transport == "stdio":
            await assert_stopped(log)


async def test_replacement_and_restore_use_current_approval_without_expanding_ceiling(
    responses: ResponsesFixture,
) -> None:
    peer = Peer(
        names=("find-record", "excluded"),
        annotations={"readOnlyHint": False, "destructiveHint": True},
    )
    async with timeout(30), peer.serve() as url:
        selected = HttpMCP(name="docs", url=url, tools=["find-record"], approval_mode="preapproved")
        agent = Agent(
            instructions="Use the selected tool.",
            model="gpt-5.5",
            provider=provider(responses),
            tools=[selected],
        )
        async with agent.session() as session:
            responses.respond(mcp_call("find_record", "mcp__docs", "approved"))
            responses.respond(assistant_output("done"))
            assert (await (await session.submit("write")).get_result()).final_output == "done"
            state = await session.save_state()
            history = [item async for item in session.items()]
            count = len(peer.requests)
            with pytest.raises(ValueError, match="captured"):
                await session.submit(
                    "select", tools=[replace(selected, tools=["find-record", "excluded"])]
                )
            assert len(peer.requests) == count
            with pytest.raises(ValueError, match="captured"):
                await session.submit(
                    "unbound policy", tools=[replace(selected, approval_mode="native")]
                )
            responses.respond(mcp_call("find_record", "mcp__docs", "denied-replacement"))
            responses.respond(assistant_output("denied"))
            turn = await session.submit("write again")
            assert (await turn.get_result()).final_output == "denied"
            assert [call["_meta"]["callId"] for call in peer.calls] == [
                "approved",
                "denied-replacement",
            ]
            responses.respond(mcp_call("find_record", "mcp__docs", "reapproved"))
            responses.respond(assistant_output("again"))
            assert (
                await (await session.submit("approved again")).get_result()
            ).final_output == "again"
        current = replace(selected, approval_mode="native")
        restored_agent = Agent(
            instructions=agent.instructions,
            model="gpt-5.5",
            provider=provider(responses),
            tools=[current],
        )
        async with restored_agent.session(state=state) as restored:
            assert [item async for item in restored.items()] == history
            responses.respond(mcp_call("find_record", "mcp__docs", "denied-restored"))
            responses.respond(assistant_output("denied after restore"))
            turn = await restored.submit("write after restore")
            cursor = restored.interactions()
            request = await anext(cursor)
            assert isinstance(request, ApprovalRequest)
            await request.respond("deny")
            await cursor.aclose()
            assert (await turn.get_result()).final_output == "denied after restore"
        assert [call["_meta"]["callId"] for call in peer.calls] == [
            "approved",
            "denied-replacement",
            "reapproved",
        ]


async def test_approval_does_not_bypass_result_policy_or_undo_the_effect(
    responses: ResponsesFixture,
) -> None:
    peer = Peer(annotations=None, label="private-write-receipt")
    policy_calls: list[str] = []

    def reject(context: MCPResultContext[None], _result: dict[str, Any]) -> dict[str, Any]:
        policy_calls.append(context.call_id)
        raise ValueError("private-policy-failure")

    async with timeout(30), peer.serve() as url:
        tool = HttpMCP(
            name="docs",
            url=url,
            tools=["search"],
            approval_mode="preapproved",
            transform_result=reject,
        )
        responses.respond(mcp_call("search", "mcp__docs", "rejected-result"))
        async with Agent(
            instructions="Use the selected tool.",
            model="gpt-5.5",
            provider=provider(responses),
            tools=[tool],
        ).session() as session:
            handle = await session.submit("write")
            with pytest.raises(NativeError, match="Native MCP result policy failed"):
                await (handle).get_result()
            assert len(peer.calls) == 1 and policy_calls == ["rejected-result"]
            history = [item async for item in session.items()]
            events = await collect(handle.events())
            for private in (peer.label, "private-policy-failure"):
                assert private not in json.dumps(history)
                assert private not in json.dumps([asdict(event) for event in events])
                assert private not in json.dumps(responses.requests)
            assert len(responses.requests) == 1
