"""Selected MCP observations share the existing passive Session stream."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agents import (
    Agent,
    ChildInvocation,
    HttpMCP,
    LocalStorage,
    MCPResultContext,
    NativeError,
    Session,
    TurnStoppedError,
    function_tool,
)
from agents.observation import (
    EventGap,
    ExecutionItemCompleted,
    Observation,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
from agents.session import _ChildCallbacks
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_agent import provider
from tests.test_native_agent_observation import collect
from tests.test_native_mcp_stdio import assert_stopped, records, stdio, wait_for_call
from tests.test_native_mcp_tools import Peer, mcp_call


def test_existing_tool_event_constructors_keep_function_default() -> None:
    started = ToolStarted(
        session_id="session",
        submission_id="submission",
        turn_id="turn",
        call_id="call",
        name="lookup",
        namespace=None,
    )
    finished = ToolFinished(
        session_id="session",
        submission_id="submission",
        turn_id="turn",
        call_id="call",
        name="lookup",
        namespace=None,
        status="completed",
    )
    assert started.family == finished.family == "function"
    assert replace(started, family="mcp").family == "mcp"
    assert replace(finished, family="mcp").family == "mcp"


@pytest.mark.parametrize("transport", ["http", "stdio"])
@pytest.mark.parametrize("mcp_first", [True, False], ids=["mcp-first", "function-first"])
async def test_selected_mcp_and_function_pairs_use_actual_native_identity(
    responses: ResponsesFixture, tmp_path: Path, transport: str, mcp_first: bool
) -> None:
    peer = Peer()
    log = tmp_path / "stdio.jsonl"

    @function_tool
    def local_lookup(key: str) -> str:
        return f"python-{key}"

    calls = [
        mcp_call("search", "mcp__records_store", "mcp-call"),
        function_call("local_lookup", {"key": "43"}, "python-call"),
    ]
    for call in calls if mcp_first else reversed(calls):
        responses.respond(call)
    responses.respond(assistant_output("done"))
    async with timeout(30), AsyncExitStack() as stack:
        if transport == "http":
            url = await stack.enter_async_context(peer.serve())
            declaration = HttpMCP(name="records-store", url=url, tools=["search"])
        else:
            declaration = replace(stdio(log), name="records-store")
        agent = Agent(
            instructions="Use selected tools.",
            model="gpt-5.5",
            provider=provider(responses),
            tools=[declaration, local_lookup],
        )
        async with agent.session() as session:
            handle = await session.submit("read")
            assert (await (handle).get_result()).final_output == "done"
            if transport == "http":
                assert [call["name"] for call in peer.calls] == ["search"]
            else:
                assert any(
                    row.get("message", {}).get("method") == "tools/call" for row in records(log)
                )
            events = await collect(handle.events())
            tools = [event for event in events if isinstance(event, (ToolStarted, ToolFinished))]
            # Native events and Python callback events use separate queues.
            # Verify each call's pair without ordering the two queues.
            assert {
                call_id: [
                    (event.family, event.type, event.name, event.namespace)
                    for event in tools
                    if event.call_id == call_id
                ]
                for call_id in {event.call_id for event in tools}
            } == {
                "mcp-call": [
                    ("mcp", "tool_started", "search", "mcp__records_store"),
                    ("mcp", "tool_finished", "search", "mcp__records_store"),
                ],
                "python-call": [
                    ("function", "tool_started", "local_lookup", None),
                    ("function", "tool_finished", "local_lookup", None),
                ],
            }
            assert all(event.turn_id == handle.turn_id for event in tools)
            assert all(event.submission_id == handle.submission_id for event in tools)
            assert all(
                event.status == "completed" for event in tools if isinstance(event, ToolFinished)
            )
            assert tools == [
                event
                for event in await collect(handle.events())
                if isinstance(event, (ToolStarted, ToolFinished))
            ]
        if transport == "stdio":
            await assert_stopped(log)


class RecordedNative:
    """Record the one existing reader's input without adding a consumer."""

    def __init__(self, native: Any) -> None:
        self.native = native
        self.events: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.native, name)

    async def _next_session_event(self) -> str:
        raw = await self.native._next_session_event()
        event = json.loads(raw)
        if event is not None:
            self.events.append(event)
        return cast(str, raw)


@pytest.mark.parametrize("outcome", ["accepted", "is_error", "peer_error", "policy_failure"])
async def test_policy_barrier_private_data_and_independent_readers(
    responses: ResponsesFixture, outcome: str
) -> None:
    peer = Peer(
        names=("find.record",),
        label="raw-private-server-result",
        is_error=outcome == "peer_error",
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def policy(ctx: MCPResultContext[None], value: dict[str, Any]) -> dict[str, Any]:
        assert ctx.tool_name == "find.record"
        assert value["content"][0]["text"] == peer.label
        assert value["isError"] is peer.is_error
        entered.set()
        await release.wait()
        if outcome == "policy_failure":
            raise ValueError("private-policy-failure")
        return {
            "content": [{"type": "text", "text": "private-accepted-result"}],
            "isError": outcome == "is_error",
        }

    call = function_call("find_record", {"key": "private-argument"}, "protected-call")
    call["item"]["namespace"] = "mcp__records_store"
    responses.respond(call)
    responses.respond(assistant_output("done"))
    async with timeout(30), peer.serve() as url:
        agent = Agent(
            instructions="Use selected tools.",
            model="gpt-5.5",
            provider=provider(responses),
            tools=[
                HttpMCP(
                    name="records-store",
                    url=url,
                    tools=["find.record"],
                    transform_result=policy,
                )
            ],
        )
        async with agent.session() as session:
            native = RecordedNative(session._native)
            session._native = native
            slow = session.events()
            handle = await session.submit("read")
            fast = asyncio.create_task(collect(handle.events()))
            reader = handle.events()
            await entered.wait()
            while not isinstance(await anext(reader), ToolStarted):
                pass
            blocked_read = asyncio.create_task(anext(reader))
            await asyncio.sleep(0)
            blocked_read.cancel()
            with pytest.raises(asyncio.CancelledError):
                await blocked_read
            assert not fast.done()
            assert len(responses.requests) == 1
            assert not any(event["msg"]["type"] == "mcp_tool_call_end" for event in native.events)
            release.set()
            if outcome == "policy_failure":
                with pytest.raises(NativeError, match="Native MCP result policy failed"):
                    await (handle).get_result()
            else:
                assert (await (handle).get_result()).final_output == "done"
            events = await fast
            assert await collect(handle.events()) == events
            starts = [event for event in events if isinstance(event, ToolStarted)]
            assert [(event.family, event.name, event.namespace) for event in starts] == [
                ("mcp", "find_record", "mcp__records_store")
            ]
            ends = [event for event in events if isinstance(event, ToolFinished)]
            native_ends = [
                event["msg"]
                for event in native.events
                if event["msg"]["type"] == "mcp_tool_call_end"
            ]
            assert [event.call_id for event in ends] == [event["call_id"] for event in native_ends]
            if outcome != "policy_failure":
                assert len(ends) == 1
                result = native_ends[0]["result"]["Ok"]
                assert result["isError"] is (outcome in ("is_error", "peer_error"))
                assert result["content"] == [{"type": "text", "text": "private-accepted-result"}]
                [completion] = [
                    event.item for event in events if isinstance(event, ExecutionItemCompleted)
                ]
                assert completion["type"] == "mcp_call"
                assert completion["thread_id"] == session.id
                assert completion["turn_id"] == handle.turn_id
                assert completion["id"] == completion["call_id"] == "protected-call"
                assert completion["arguments"] == {"key": "private-argument"}
                assert completion["output"] == result
                assert completion["native_status"] == ends[0].status
                history = [item async for item in session.items()]
                assert [item for item in history if item["type"] == "mcp_call"] == [completion]
                outputs = [
                    item["output"]
                    for item in history
                    if item["type"] == "function_call_output"
                    and item["call_id"] == "protected-call"
                ]
                assert len(outputs) == 1
                assert "private-accepted-result" in str(outputs[0])
                assert peer.label not in str(outputs[0])
                assert len(responses.requests) == 2
                model_output = next(
                    item["output"]
                    for item in responses.requests[1]["input"]
                    if item["type"] == "function_call_output"
                    and item["call_id"] == "protected-call"
                )
                assert "private-accepted-result" in str(model_output)
                assert peer.label not in str(model_output)
            assert all(
                event.status == ("completed" if outcome == "accepted" else "failed")
                for event in ends
            )
            assert all(event.turn_id == handle.turn_id for event in [*starts, *ends])
            assert isinstance(events[-1], TurnFinished)
            assert events[-1].status == ("failed" if outcome == "policy_failure" else "completed")
            public = json.dumps([asdict(event) for event in events])
            assert peer.label not in public
            assert "private-policy-failure" not in public
            statuses = json.dumps(
                [asdict(event) for event in events if not isinstance(event, ExecutionItemCompleted)]
            )
            assert "private-argument" not in statuses
            assert "private-accepted-result" not in statuses
            await session._aclose()
            assert (await collect(slow))[:-1] == events


async def test_large_mcp_completion_recovers_from_gap_after_local_reopen(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    peer = Peer(label="x" * 100_000)
    responses.respond(mcp_call("search", "mcp__records", "large-call"))
    responses.respond(assistant_output("done"))
    async with timeout(30), peer.serve() as url:
        storage = LocalStorage(tmp_path / "profile")
        agent = Agent(
            instructions="Use selected tools.",
            provider=provider(responses),
            tools=[HttpMCP(name="records", url=url, tools=["search"])],
        )
        async with agent.session(storage=storage) as session:
            thread_id = session.id
            turn = await session.submit("read")
            assert (await turn.get_result()).final_output == "done"
            turn_id = turn.turn_id
            observed = await collect(turn.events())
            assert isinstance(observed[0], EventGap)
            assert isinstance(observed[-1], TurnFinished)
        async with agent.session(storage=storage, resume=thread_id) as session:
            [retained] = [item async for item in session.items() if item["type"] == "mcp_call"]
            assert retained["type"] == "mcp_call"
            assert (retained["thread_id"], retained["turn_id"]) == (thread_id, turn_id)
            assert retained["id"] == retained["call_id"] == "large-call"
            assert retained["arguments"] == {"key": "43"}
            assert retained["native_status"] == "completed"
            assert "chars truncated" in str(retained["output"])
            encoded = json.dumps(retained["output"], ensure_ascii=False, separators=(",", ":"))
            assert len(encoded.encode()) <= 65_536


async def test_cancelled_stdio_call_publishes_only_delivered_native_ends(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    log = tmp_path / "held-stdio.jsonl"
    responses.respond(mcp_call("search", "mcp__local", "cancel-call"))
    async with timeout(30):
        agent = Agent(
            instructions="Use selected tools.",
            model="gpt-5.5",
            provider=provider(responses),
            tools=[stdio(log, "--hold")],
        )
        async with agent.session() as session:
            native = RecordedNative(session._native)
            session._native = native
            handle = await session.submit("read")
            reader = handle.events()
            await wait_for_call(log)
            while not isinstance(await anext(reader), ToolStarted):
                pass
            await handle.cancel()
            with pytest.raises(TurnStoppedError):
                await (handle).get_result()
            events = await collect(handle.events())
            ends = [event for event in events if isinstance(event, ToolFinished)]
            native_ends = [
                event["msg"]
                for event in native.events
                if event["msg"]["type"] == "mcp_tool_call_end"
            ]
            assert [event.call_id for event in ends] == [event["call_id"] for event in native_ends]
            assert all(event.status == "failed" for event in ends)
            observed = [event.item for event in events if isinstance(event, ExecutionItemCompleted)]
            assert observed == [
                item async for item in session.items() if item["type"] == "mcp_call"
            ]
            assert all(item["turn_id"] == handle.turn_id for item in observed)
            assert isinstance(events[-1], TurnFinished)
            assert events[-1].status == "cancelled"
            assert len(responses.requests) == 1
        await assert_stopped(log)


@pytest.mark.parametrize(
    "boundary", ["child", "stale", "empty", "absent", "server", "tool", "kind"]
)
def test_mcp_projection_excludes_unowned_events(boundary: str) -> None:
    events: list[Observation] = []
    owner = SimpleNamespace(
        id="session",
        _mcp_tools=(
            HttpMCP(name="records-store", url="http://localhost/mcp", tools=["find.record"]),
        ),
        _observations=SimpleNamespace(append=events.append),
    )
    handle = SimpleNamespace(turn_id="turn", submission_id="submission")
    state = SimpleNamespace(handle=handle)
    event = {
        "id": "turn",
        "msg": {
            "type": "mcp_tool_call_begin",
            "turn_id": "turn",
            "call_id": "call",
            "invocation": {
                "server": "records-store",
                "tool": "find.record",
                "arguments": "private",
            },
        },
    }
    rejected: dict[str, Any] = deepcopy(event)
    if boundary == "child":
        rejected["subagent_id"] = "child"
    elif boundary in {"stale", "empty"}:
        rejected["msg"]["turn_id"] = "old-turn" if boundary == "stale" else ""
    elif boundary == "absent":
        rejected["msg"].pop("turn_id")
    elif boundary in {"server", "tool"}:
        rejected["msg"]["invocation"][boundary] = "unselected"
    else:
        rejected["msg"]["type"] = "item_started"
    Session._observe_mcp(cast(Any, owner), rejected, cast(Any, state))
    assert events == []
    Session._observe_mcp(cast(Any, owner), event, cast(Any, state))
    assert len(events) == 1
    assert isinstance(events[0], ToolStarted)
    assert events[0].family == "mcp"
    assert "private" not in repr(events)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"Ok": {}}, "completed"),
        ({"Ok": {"isError": False}}, "completed"),
        ({"Ok": {"isError": True}}, "failed"),
        ({"Err": "private-error"}, "failed"),
    ],
)
def test_mcp_end_classification_uses_native_result(result: dict[str, Any], expected: str) -> None:
    events: list[Observation] = []
    owner = SimpleNamespace(
        id="session",
        _mcp_tools=(HttpMCP(name="docs", url="http://localhost/mcp", tools=["search"]),),
        _observations=SimpleNamespace(append=events.append),
    )
    event = {
        "msg": {
            "type": "mcp_tool_call_end",
            "turn_id": "turn",
            "call_id": "call",
            "invocation": {"server": "docs", "tool": "search"},
            "result": result,
        }
    }
    Session._observe_mcp(
        cast(Any, owner),
        event,
        cast(
            Any, SimpleNamespace(handle=SimpleNamespace(turn_id="turn", submission_id="submission"))
        ),
    )
    assert len(events) == 1
    assert isinstance(events[0], ToolFinished)
    assert events[0].status == expected
    assert "private-error" not in repr(events)


@pytest.mark.parametrize("boundary", ["child", "incarnation", "root"])
async def test_child_mcp_projection_requires_exact_owner(boundary: str) -> None:
    events: list[Observation] = []
    owner = SimpleNamespace(
        id="session",
        _mcp_tools=(HttpMCP(name="records", url="http://localhost/mcp", tools=["find"]),),
        _observations=SimpleNamespace(append=events.append),
    )
    state = _ChildCallbacks(ChildInvocation("session", "child", "instance", "turn"))
    event = {
        "id": "turn",
        "subagent_id": "child",
        "subagent_incarnation": "instance",
        "msg": {
            "type": "mcp_tool_call_begin",
            "turn_id": "turn",
            "call_id": "call",
            "invocation": {"server": "records", "tool": "find", "arguments": "private"},
        },
    }
    rejected: dict[str, Any] = deepcopy(event)
    if boundary == "root":
        rejected.pop("subagent_id")
        rejected.pop("subagent_incarnation")
    else:
        rejected["subagent_id" if boundary == "child" else "subagent_incarnation"] = "other"
    Session._observe_mcp(cast(Any, owner), rejected, state)
    assert events == []
    Session._observe_mcp(cast(Any, owner), event, state)
    assert len(events) == 1
    assert isinstance(events[0], ToolStarted)
    assert events[0].submission_id == state.handle.submission_id
