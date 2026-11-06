"""Environment policy edits while a non-tool provider item is still open."""

import asyncio
import json

import pytest

from agents import Agent, Allow, NativeError, ReplaceArguments, StopTurn, TurnStoppedError
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_child_agents import trust
from tests.test_native_executor_tools import declaration
from tests.test_native_provider_websockets import peer as websocket_peer


def _added(done, index):
    item = dict(done["item"])
    if item["type"] == "function_call":
        item["arguments"] = ""
    elif item["type"] == "custom_tool_call":
        item["input"] = ""
    return {"type": "response.output_item.added", "item": item, "output_index": index}


def _overlap(tool="exec_command"):
    message = assistant_output("I will inspect the workspace.")
    message["item"]["phase"] = "commentary"
    if tool == "exec_command":
        first = function_call(tool, {"cmd": "printf barrier-original-first"}, "barrier-first")
    else:
        first = {
            "type": "response.output_item.done",
            "item": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "call_id": "barrier-first",
                # A valid update to a missing file exercises Core's verifier and
                # real error output, without requesting a host OS permission.
                "input": "*** Begin Patch\n*** Update File: barrier-missing\n"
                "@@\n-old\n+barrier-original-first\n*** End Patch",
            },
        }
    first["item"]["id"] = ("fc_" if tool == "exec_command" else "ctc_") + "barrierFirstProvider"
    return message, first


@pytest.mark.parametrize("tool", ["exec_command", "apply_patch"])
async def test_environment_interleaved_edit_keeps_later_calls_in_order(tmp_path, monkeypatch, tool):
    admission = []

    def before(ctx, value):
        admission.append(("before", ctx.call_id))
        content = value["cmd"] if isinstance(value, dict) else value
        if "barrier-original-" not in content:
            return Allow()
        edited = content.replace("barrier-original-", "barrier-accepted-")
        return ReplaceArguments({**value, "cmd": edited} if isinstance(value, dict) else edited)

    def approval(ctx, value):
        admission.append(("approval", ctx.call_id))
        if ctx.call_id == "barrier-mid":
            assert value == {"cmd": "printf barrier-allowed-middle"}
        else:
            assert f"barrier-original-{ctx.call_id.removeprefix('barrier-')}" in str(value)
        return True

    async with (
        timeout(40),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        websocket_peer() as model,
    ):
        trust(executor, tmp_path, monkeypatch)
        message, first = _overlap(tool)
        middle = function_call(
            "exec_command", {"cmd": "printf barrier-allowed-middle"}, "barrier-mid"
        )
        middle["item"]["id"] = "fc_barrierMidProvider"
        last = function_call(
            "exec_command", {"cmd": "printf barrier-original-last"}, "barrier-last"
        )
        last["item"]["id"] = "fc_barrierLastProvider"
        calls = [first, middle, last]
        model.respond(
            _added(message, 0),
            _added(first, 1),
            {**first, "output_index": 1},
            _added(middle, 2),
            {**middle, "output_index": 2},
            _added(last, 3),
            {**last, "output_index": 3},
            {**message, "output_index": 0},
        )
        model.respond(assistant_output("finished"))
        agent = Agent(instructions="Use environment tools.", tools=[], provider=model.provider())
        async with agent.session(
            environment=declaration(executor, transform_arguments=before, approval=approval)
        ) as session:
            assert (await (await session.submit("Check")).get_result()).final_output == "finished"
            names = [event["item"]["call_id"] for event in calls]
            assert admission == [
                (phase, name) for name in names for phase in ("approval", "before")
            ]
            state = await session.save_state()
            saved = [row["payload"] for row in state.history if row["type"] == "response_item"]
            public = [item async for item in session.items()]
            wire = model.requests[-1]["input"]
            wire_calls = [
                item
                for item in wire
                if item.get("type") in ("function_call", "custom_tool_call")
                and item.get("call_id") in names
            ]
            assert [item["call_id"] for item in wire_calls] == names
            for original, received in zip(calls, wire_calls, strict=True):
                call_id = received["call_id"]
                stored = [
                    item
                    for item in saved
                    if item.get("call_id") == call_id
                    and item.get("type") in ("function_call", "custom_tool_call")
                ]
                projected = [
                    item
                    for item in public
                    if item.get("call_id") == call_id
                    and item.get("type") in ("function_call", "custom_tool_call")
                ]
                assert len(stored) == len(projected) == 1
                assert stored[0]["id"] == projected[0]["id"] == received["id"]
                assert received["id"] != message["item"]["id"]
                expected = original["item"].get("arguments", original["item"].get("input"))
                expected = expected.replace("barrier-original-", "barrier-accepted-")
                if received["type"] == "function_call":
                    assert json.loads(received["arguments"]) == json.loads(expected)
                    assert json.loads(projected[0]["arguments"]) == json.loads(expected)
                else:
                    assert received["input"] == projected[0]["arguments"] == expected
                assert any(
                    item.get("call_id") == call_id
                    and item.get("type") in ("function_call_output", "custom_tool_call_output")
                    for item in wire
                )
            assert len({item["id"] for item in wire_calls}) == len(calls)
            assert wire_calls[1]["id"] == middle["item"]["id"]
            assert any(item.get("id") == message["item"]["id"] for item in public)
            assert not model.requests[-1].get("previous_response_id"), model.frames
            assert model.connections == 1 and not model.http_requests


@pytest.mark.parametrize("terminal", ["unclosed-completed", "provider-failure"])
async def test_environment_interleaved_edit_fails_without_drain(tmp_path, monkeypatch, terminal):
    async with (
        timeout(35),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        websocket_peer() as model,
    ):
        trust(executor, tmp_path, monkeypatch)
        message, first = _overlap()
        tail = []
        if terminal == "provider-failure":
            tail.append(
                {
                    "type": "response.failed",
                    "response": {
                        "error": {
                            "type": "invalid_request_error",
                            "code": "invalid_prompt",
                            "message": "synthetic barrier request rejected",
                        }
                    },
                }
            )
        # WebSocketPeer sends the real response.completed automatically; the
        # message Added remains unclosed when that terminal arrives.
        model.respond(_added(message, 0), _added(first, 1), {**first, "output_index": 1}, *tail)
        # If the broken turn continues, finish it promptly so the failure is
        # observable success, not a fixture exhaustion timeout.
        model.respond(assistant_output("unexpected success"))
        policy = {
            "transform_arguments": lambda ctx, value: ReplaceArguments(
                {"cmd": "printf barrier-accepted-first"}
            )
        }
        agent = Agent(instructions="Use environment tools.", tools=[], provider=model.provider())
        async with agent.session(environment=declaration(executor, **policy)) as session:
            with pytest.raises(NativeError):
                await (await session.submit("Check")).get_result()
            assert not [
                r
                for r in executor.requests
                if r.get("method") == "process/start" and "barrier-" in str(r.get("params"))
            ]
            assert len(model.requests) == 1
            state = await session.save_state()
            assert not any(
                row["type"] == "response_item" and row["payload"].get("call_id") == "barrier-first"
                for row in state.history
            )
            assert not model.http_requests


@pytest.mark.parametrize("interruption", ["cancel", "stop-approval"])
async def test_environment_interleaved_edit_interrupted_before_drain(
    tmp_path, monkeypatch, interruption
):
    gate, release, decided = asyncio.Event(), asyncio.Event(), asyncio.Event()

    def approval(ctx, value):
        assert value == {"cmd": "printf barrier-original-first"}
        decided.set()
        return StopTurn("private barrier reason") if interruption == "stop-approval" else True

    async with (
        timeout(35),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        websocket_peer() as model,
    ):
        trust(executor, tmp_path, monkeypatch)
        message, first = _overlap()

        async def hold(event):
            if event.get("type") == "response.output_item.done" and (
                event.get("item", {}).get("id") == message["item"]["id"]
            ):
                gate.set()
                await release.wait()
                # End this synthetic socket after the local turn has joined.
                raise asyncio.CancelledError

        model.before_event = hold
        model.respond(
            _added(message, 0),
            _added(first, 1),
            {**first, "output_index": 1},
            {**message, "output_index": 0},
        )
        policy = {
            "transform_arguments": lambda ctx, value: ReplaceArguments(
                {"cmd": "printf barrier-accepted-first"}
            ),
            "approval": approval,
        }
        agent = Agent(instructions="Use environment tools.", tools=[], provider=model.provider())
        async with agent.session(environment=declaration(executor, **policy)) as session:
            turn = await session.submit("Check")
            try:
                await gate.wait()
                await decided.wait()
                if interruption == "cancel":
                    await turn.cancel()
                    with pytest.raises(TurnStoppedError, match="^Turn stopped$"):
                        await turn.get_result()
                else:
                    with pytest.raises(TurnStoppedError, match="^Turn stopped by policy$"):
                        await turn.get_result()
                assert not [
                    r
                    for r in executor.requests
                    if r.get("method") == "process/start" and "barrier-" in str(r.get("params"))
                ]
                state = await session.save_state()
                assert not any(
                    row["type"] == "response_item"
                    and row["payload"].get("call_id") == "barrier-first"
                    for row in state.history
                )
                assert len(model.requests) == 1 and not model.http_requests
            finally:
                release.set()


async def test_environment_interleaved_edit_finite_pending_limit(tmp_path, monkeypatch):
    """A modest stream over the supported 128-pending budget cannot partly execute."""
    admitted = []

    def before(ctx, value):
        admitted.append(ctx.call_id)
        return ReplaceArguments({"cmd": "printf barrier-accepted-first"})

    async with (
        timeout(40),
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        websocket_peer() as model,
    ):
        trust(executor, tmp_path, monkeypatch)
        message, first = _overlap()
        following = []
        # Actual fixture input: 129 small calls, no large allocation and no
        # private barrier inspection. The message closes only after all calls.
        for index in range(129):
            event = function_call(
                "exec_command", {"cmd": f"printf barrier-overflow-{index}"}, f"barrier-more-{index}"
            )
            event["item"]["id"] = f"fc_barrierOverflow{index}"
            following.extend((_added(event, index + 2), {**event, "output_index": index + 2}))
        model.respond(
            _added(message, 0),
            _added(first, 1),
            {**first, "output_index": 1},
            *following,
            {**message, "output_index": 0},
        )
        model.respond(assistant_output("unexpected success"))
        agent = Agent(instructions="Use environment tools.", tools=[], provider=model.provider())
        async with agent.session(
            environment=declaration(executor, transform_arguments=before)
        ) as session:
            with pytest.raises(NativeError):
                await (await session.submit("Check")).get_result()
            assert admitted == ["barrier-first"]
            assert not [
                r
                for r in executor.requests
                if r.get("method") == "process/start" and "barrier-" in str(r.get("params"))
            ]
            state = await session.save_state()
            assert not any(
                row["type"] == "response_item"
                and str(row["payload"].get("call_id", "")).startswith("barrier-")
                for row in state.history
            )
            assert len(model.requests) == 1 and not model.http_requests
