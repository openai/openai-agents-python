"""Direct root StopTurn decisions prevent dispatch, including with result policies."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import aclosing

import pytest

from agents import Agent, HttpMCP, NativeError, StopTurn, TurnStoppedError
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_child_agents import trust
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_tools import Peer, mcp_call

PRIVATE_REASON = "synthetic-direct-stop-private-056a"
DIRECT_DENIAL = "Native tool interception failed or was denied"


@pytest.mark.parametrize("stop", [False, True], ids=["native-deny", "stop-turn"])
async def test_direct_root_mcp_before_decision_is_owned_and_never_reaches_peer(
    responses: ResponsesFixture, stop: bool
) -> None:
    entered, decide, left = asyncio.Event(), asyncio.Event(), asyncio.Event()
    notices = []

    async def approval(context, call):
        notices.append((context, call))
        entered.set()
        try:
            await decide.wait()
            return StopTurn(PRIVATE_REASON) if stop else False
        finally:
            left.set()

    peer = Peer(names=("find-record",))
    responses.respond(mcp_call("find_record", "mcp__vault", "selected-direct"))
    responses.respond(assistant_output("next-explicit-turn"))
    async with timeout(35), peer.serve() as url, aclosing(provider(responses)) as native:
        async with Agent(
            instructions="Use selected tools.",
            provider=native,
            tools=[
                HttpMCP(
                    name="vault",
                    url=url,
                    tools=["find-record"],
                    approval=approval,
                )
            ],
        ).session() as session:
            turn = await session.submit("attempt selected")
            waiter = asyncio.create_task(turn.get_result())
            try:
                await entered.wait()
                [(context, call)] = notices
                assert context.submission is turn and context.call.call_id == "selected-direct"
                assert call.thread_id == session.id and call.turn_id == turn.turn_id
                assert call.name == "find_record" and call.namespace == "mcp__vault"
                assert call.route == "direct" and call.call_id == "selected-direct"
                assert call.outer_call_id is call.cell_id is call.runtime_tool_call_id is None
                assert peer.calls == [] and len(responses.requests) == 1
                assert not waiter.done() and not left.is_set()
                decide.set()
                message = "^Turn stopped by policy$" if stop else DIRECT_DENIAL
                expected_error = TurnStoppedError if stop else NativeError
                with pytest.raises(expected_error, match=message) as caught:
                    await waiter
                assert left.is_set()
                assert peer.calls == [] and len(responses.requests) == 1
                assert PRIVATE_REASON not in str(caught.value)
                assert PRIVATE_REASON not in str(caught.value.__context__) + str(
                    caught.value.__cause__
                )
                assert PRIVATE_REASON not in str([item async for item in session.items()])
                assert PRIVATE_REASON not in json.dumps(responses.requests)
                if not stop:
                    assert "Turn stopped by policy" not in str(caught.value)
                fresh = await session.submit("new explicit turn")
                assert fresh.turn_id != turn.turn_id
                assert (await fresh.get_result()).final_output == "next-explicit-turn"
                with pytest.raises(expected_error, match=message):
                    await turn.get_result()
                assert (await fresh.get_result()).final_output == "next-explicit-turn"
                assert peer.calls == [] and len(responses.requests) == 2
            finally:
                decide.set()
                await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize(
    "kind",
    [
        pytest.param("exec_command", id="exec-approval"),
        pytest.param("apply_patch", id="patch-transform"),
        pytest.param("write_stdin", id="stdin-approval"),
    ],
)
async def test_direct_root_executor_stop_prevents_selected_effect_and_continuation(
    responses: ResponsesFixture, tmp_path, monkeypatch, kind: str
) -> None:
    notices = []
    left = asyncio.Event()
    baseline_writes = []

    async def before(context, value):
        if context.tool_name != kind:
            return True
        notices.append((context, value))
        try:
            return StopTurn(PRIVATE_REASON)
        finally:
            left.set()

    async with (
        timeout(65),
        ExecutorPeer(tmp_path / "remote").serve() as peer,
        aclosing(provider(responses)) as native,
    ):
        trust(peer, tmp_path, monkeypatch)
        if kind == "write_stdin":
            responses.respond(
                function_call(
                    "exec_command",
                    {
                        "cmd": "read -r value; printf '%s' \"$value\" >> direct-effect",
                        "tty": True,
                        "yield_time_ms": 1000,
                    },
                    "start-process",
                )
            )
            responses.respond(function_call(kind, {}, "selected-direct"))

            async def supply_process_id(event):
                if event.get("item", {}).get("call_id") == "selected-direct":
                    output = tool_output(responses.requests[-1], "start-process")
                    match = re.search(r"Process running with session ID (\d+)", output)
                    assert match, output
                    baseline_writes.append(
                        sum(r.get("method") == "process/write" for r in peer.requests)
                    )
                    event["item"]["arguments"] = json.dumps(
                        {
                            "session_id": int(match[1]),
                            "chars": "forbidden\n",
                            "yield_time_ms": 1000,
                        }
                    )

            responses.before_event = supply_process_id
        elif kind == "apply_patch":
            responses.respond(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "custom_tool_call",
                        "name": "apply_patch",
                        "call_id": "selected-direct",
                        "input": (
                            "*** Begin Patch\n*** Add File: direct-effect\n"
                            "+forbidden\n*** End Patch"
                        ),
                    },
                }
            )
        else:
            responses.respond(
                function_call(kind, {"cmd": "printf forbidden >> direct-effect"}, "selected-direct")
            )
        responses.respond(assistant_output("next-explicit-turn"))
        selected = (
            declaration(peer, transform_arguments=before)
            if kind == "apply_patch"
            else declaration(peer, approval=before)
        )
        async with Agent(instructions="Use selected tools.", tools=[], provider=native).session(
            environment=selected
        ) as session:
            turn = await session.submit("attempt selected")
            with pytest.raises(TurnStoppedError, match="^Turn stopped by policy$") as caught:
                await turn.get_result()
            [(context, value)] = notices
            assert context.submission is turn and context.session is session
            assert context.thread_id == session.id and context.turn_id == turn.turn_id
            assert context.tool_name == kind and context.call_id == "selected-direct"
            assert context.outer_call_id is context.cell_id is context.runtime_tool_call_id is None
            assert left.is_set()
            assert not (peer.root / "direct-effect").exists()
            before_requests = 2 if kind == "write_stdin" else 1
            assert len(responses.requests) == before_requests
            if kind == "write_stdin":
                assert [
                    sum(r.get("method") == "process/write" for r in peer.requests)
                ] == baseline_writes
            assert PRIVATE_REASON not in str(caught.value)
            assert PRIVATE_REASON not in str(caught.value.__context__) + str(caught.value.__cause__)
            assert PRIVATE_REASON not in str([item async for item in session.items()])
            assert PRIVATE_REASON not in json.dumps(responses.requests)
            fresh = await session.submit("new explicit turn")
            assert fresh.turn_id != turn.turn_id
            assert (await fresh.get_result()).final_output == "next-explicit-turn"
            with pytest.raises(TurnStoppedError, match="^Turn stopped by policy$"):
                await turn.get_result()
            assert len(responses.requests) == before_requests + 1
            assert not (peer.root / "direct-effect").exists()


@pytest.mark.requires_native_linux_sandbox
async def test_direct_executor_result_policy_stop_prevents_dispatch_and_result(
    responses: ResponsesFixture, tmp_path, monkeypatch
) -> None:
    policies = []

    async def approval(context, value):
        policies.append("approval")
        return StopTurn(PRIVATE_REASON)

    async def result(context, value):
        policies.append("result")
        return value

    async with (
        timeout(60),
        ExecutorPeer(tmp_path / "remote").serve() as peer,
        aclosing(provider(responses)) as native,
    ):
        trust(peer, tmp_path, monkeypatch)
        responses.respond(
            function_call(
                "exec_command", {"cmd": "printf bypass >> direct-gated-effect"}, "selected-direct"
            )
        )
        responses.respond(assistant_output("next-explicit-turn"))
        async with Agent(
            instructions="Use selected tools.",
            tools=[],
            provider=native,
            programmatic_tool_calling=True,
        ).session(
            environment=declaration(peer, approval=approval, transform_result=result)
        ) as session:
            turn = await session.submit("stop a direct call with a result policy")
            with pytest.raises(TurnStoppedError, match="^Turn stopped by policy$") as caught:
                await turn.get_result()
            assert type(caught.value) is TurnStoppedError
            assert PRIVATE_REASON not in str(caught.value)
            assert policies == ["approval"]
            assert not (peer.root / "direct-gated-effect").exists()
            assert len(responses.requests) == 1
            assert not any(
                "direct-gated-effect" in json.dumps(request) for request in peer.requests
            )
