"""Native before decisions must stop their exact turn, or deny just one call."""

from __future__ import annotations

import asyncio
import json
import re
import traceback
from contextlib import aclosing
from dataclasses import FrozenInstanceError

import pytest

from agents import (
    Agent,
    HttpMCP,
    NativeProvider,
    ReplaceArguments,
    StopTurn,
    TurnStoppedError,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_child_agents import trust
from tests.test_native_code_mode import code_call
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_result_policies import result
from tests.test_native_mcp_tools import Peer

PRIVATE_REASON = "synthetic private policy reason-c862"


def test_stop_turn_is_the_agreed_immutable_decision():
    decision = StopTurn(PRIVATE_REASON)
    assert decision.reason == PRIVATE_REASON
    with pytest.raises(FrozenInstanceError):
        decision.reason = "another reason"
    with pytest.raises(TypeError):
        raise decision


@pytest.mark.parametrize("stop", [False, True], ids=["local-deny", "stop-turn"])
async def test_mcp_code_decision_joins_owned_cleanup_and_preserves_other_turns(
    responses: ResponsesFixture, stop, caplog
):
    started = {name: asyncio.Event() for name in ("owned", "other")}
    released = {name: asyncio.Event() for name in started}
    cleaned = {name: asyncio.Event() for name in started}
    cleaning = asyncio.Event()
    reached, decide = asyncio.Event(), asyncio.Event()
    notices = []

    @function_tool
    async def held(key: str) -> str:
        started[key].set()
        try:
            await released[key].wait()
            return "receipt-" + key
        finally:
            if key == "owned":
                cleaning.set()
                await released[key].wait()
            cleaned[key].set()

    async def approval(context, call):
        await started["owned"].wait()
        notices.append((context, call))
        reached.set()
        await decide.wait()
        return StopTurn(PRIVATE_REASON) if stop else False

    chosen = Peer(names=("find-record",), label="chosen")
    ordinary = Peer(names=("find-record",), label="ordinary")
    responses.respond(
        code_call(
            'const owned = tools.held({key:"owned"});'
            'try { await tools.mcp__vault__find_record({key:"43"}); } catch (denial) {}'
            'await tools.mcp__ordinary__find_record({key:"plain"});'
            'text("AFTER-SELECTED:" + await owned);',
            "policy-cell",
        )
    )
    responses.respond(function_call("held", {"key": "other"}, "unrelated-call"))
    async with (
        timeout(45),
        chosen.serve() as chosen_url,
        ordinary.serve() as ordinary_url,
        aclosing(
            NativeProvider(
                base_url=responses.url, api_key="synthetic-policy", supports_websockets=False
            )
        ) as provider,
    ):
        agent = Agent(
            instructions="Use selected tools.",
            tools=[
                held,
                HttpMCP(
                    name="vault",
                    url=chosen_url,
                    tools=["find-record"],
                    approval=approval,
                ),
                HttpMCP(name="ordinary", url=ordinary_url, tools=["find-record"]),
            ],
            provider=provider,
            programmatic_tool_calling=True,
        )
        unrelated = Agent(instructions="Use held.", tools=[held], provider=provider)
        async with agent.session() as session, unrelated.session() as other:
            turn = await session.submit("start")
            waiter = asyncio.create_task(turn.get_result())
            other_waiter = None
            try:
                await reached.wait()
                other_turn = await other.submit("keep unrelated work alive")
                other_waiter = asyncio.create_task(other_turn.get_result())
                await started["other"].wait()
                [(context, call)] = notices
                assert context.submission is turn
                assert call.thread_id == session.id
                assert call.turn_id == turn.turn_id
                assert call.outer_call_id == "policy-cell"
                assert call.cell_id and call.runtime_tool_call_id
                assert call.name == "find_record" and call.namespace == "mcp__vault"
                assert chosen.calls == ordinary.calls == []
                assert len(responses.requests) == 2
                decide.set()
                if stop:
                    await cleaning.wait()
                    assert not waiter.done()
                    assert not cleaned["owned"].is_set()
                    assert not other_waiter.done()
                    assert not cleaned["other"].is_set()
                    assert chosen.calls == ordinary.calls == []
                    assert len(responses.requests) == 2
                    released["owned"].set()
                    with pytest.raises(
                        TurnStoppedError, match="^Turn stopped by policy$"
                    ) as caught:
                        await waiter
                    error = caught.value
                    assert PRIVATE_REASON not in str(error) + repr(error)
                    assert PRIVATE_REASON not in str(error.__context__) + str(error.__cause__)
                    assert PRIVATE_REASON not in "".join(traceback.format_exception(error))
                    assert chosen.calls == ordinary.calls == []
                    assert len(responses.requests) == 2
                else:
                    responses.respond(assistant_output("local-deny-completed"))
                    released["owned"].set()
                    assert (await waiter).final_output == "local-deny-completed"
                    assert chosen.calls == []
                    assert [(c["name"], c["arguments"]) for c in ordinary.calls] == [
                        ("find-record", {"key": "plain"})
                    ]
                    assert "AFTER-SELECTED:receipt-owned" in json.dumps(responses.requests[-1])
                assert cleaned["owned"].is_set()
                assert not other_waiter.done() and not cleaned["other"].is_set()
                responses.respond(assistant_output("unrelated-survived"))
                released["other"].set()
                assert (await other_waiter).final_output == "unrelated-survived"
                assert cleaned["other"].is_set()
                items = [item async for item in session.items()]
                history = str(items)
                assert PRIVATE_REASON not in history
                assert PRIVATE_REASON not in caplog.text
                assert PRIVATE_REASON not in json.dumps(responses.requests)
                if stop:
                    assert "AFTER-SELECTED:" not in str(
                        [item for item in items if item.get("type") == "function_call_output"]
                    )
                    responses.respond(assistant_output("newer-survived"))
                    fresh = await session.submit("explicit-new-turn")
                    assert fresh is not turn and fresh.turn_id != turn.turn_id
                    assert (await fresh.get_result()).final_output == "newer-survived"
                    with pytest.raises(TurnStoppedError, match="^Turn stopped by policy$"):
                        await turn.get_result()
                    assert (await fresh.get_result()).final_output == "newer-survived"
                    assert (await other_turn.get_result()).final_output == "unrelated-survived"
            finally:
                decide.set()
                for event in released.values():
                    event.set()
                await asyncio.gather(
                    *(task for task in (waiter, other_waiter) if task is not None),
                    return_exceptions=True,
                )


async def test_mcp_true_approval_then_result_transform_reaches_native_consumer(
    responses: ResponsesFixture,
):
    approve_entered, approve_release = asyncio.Event(), asyncio.Event()
    result_entered, result_release = asyncio.Event(), asyncio.Event()
    before, after = [], []
    selected = Peer(names=("find-record",), label="private-result-before-transform-725f")
    ordinary = Peer(names=("find-record",), label="ordinary-unmodified-receipt-162d")

    async def approve(context, call):
        before.append((context, call))
        approve_entered.set()
        await approve_release.wait()
        return True

    async def redact(context, value):
        after.append((context, value))
        result_entered.set()
        await result_release.wait()
        return result("accepted-result-610a")

    responses.respond(
        code_call(
            'const a = await tools.mcp__vault__find_record({key:"selected"});'
            'const b = await tools.mcp__ordinary__find_record({key:"plain"});'
            'text("APPROVED-RESULT:" + JSON.stringify([a,b]));',
            "approved-cell",
        )
    )
    responses.respond(assistant_output("approved-done"))
    async with (
        timeout(35),
        selected.serve() as selected_url,
        ordinary.serve() as ordinary_url,
        aclosing(
            NativeProvider(
                base_url=responses.url, api_key="synthetic-approved-mcp", supports_websockets=False
            )
        ) as provider,
    ):
        agent = Agent(
            instructions="Use native tools.",
            tools=[
                HttpMCP(
                    name="vault",
                    url=selected_url,
                    tools=["find-record"],
                    approval=approve,
                    transform_result=redact,
                ),
                HttpMCP(name="ordinary", url=ordinary_url, tools=["find-record"]),
            ],
            provider=provider,
            programmatic_tool_calling=True,
        )
        async with agent.session() as session:
            turn = await session.submit("read both")
            waiter = asyncio.create_task(turn.get_result())
            try:
                await approve_entered.wait()
                [(owner, call)] = before
                assert owner.submission is turn
                assert call.thread_id == session.id and call.turn_id == turn.turn_id
                assert call.outer_call_id == "approved-cell"
                assert call.cell_id and call.runtime_tool_call_id
                assert owner.call.call_id is None
                assert owner.call.nested_call_id == call.runtime_tool_call_id
                assert call.name == "find_record" and call.namespace == "mcp__vault"
                assert selected.calls == ordinary.calls == after == []
                assert len(responses.requests) == 1 and not waiter.done()
                approve_release.set()
                await result_entered.wait()
                assert len(selected.calls) == 1 and ordinary.calls == []
                assert len(responses.requests) == 1 and not waiter.done()
                [(consumer, original)] = after
                assert consumer.submission is turn
                assert (consumer.server_name, consumer.tool_name) == ("vault", "find-record")
                assert consumer.call_id == selected.calls[0]["_meta"]["callId"]
                assert selected.calls[0]["arguments"] == {"key": "selected"}
                assert original["content"] == [{"type": "text", "text": selected.label}]
                assert original["structuredContent"] == {"receipt": selected.label}
                assert original["isError"] is False
            finally:
                approve_release.set()
                result_release.set()
                await asyncio.gather(waiter, return_exceptions=True)
            assert (await waiter).final_output == "approved-done"
            assert len(before) == len(after) == len(selected.calls) == len(ordinary.calls) == 1
            assert ordinary.calls[0]["arguments"] == {"key": "plain"}
            assert len(responses.requests) == 2
            wire = responses.requests[-1]["input"]
            history = [item async for item in session.items()]
            [wire_output] = [
                item
                for item in wire
                if item.get("type") == "custom_tool_call_output"
                and item.get("call_id") == "approved-cell"
            ]
            [public_output] = [
                item
                for item in history
                if item.get("type") == "function_call_output"
                and item.get("call_id") == "approved-cell"
            ]
            assert public_output["id"] == wire_output["id"]
            assert public_output["turn_id"] == turn.turn_id
            assert public_output["status"] == "completed"
            assert public_output["output"] == wire_output["output"]
            assert "APPROVED-RESULT:" in str(wire_output["output"])
            assert "accepted-result-610a" in str(wire_output["output"])
            assert ordinary.label in str(wire_output["output"])
            for observable in (
                json.dumps(responses.requests),
                str(history),
                (await session.save_state()).to_json(),
            ):
                assert selected.label not in observable


async def test_cancelled_mcp_approval_cannot_stop_another_or_successor_turn(
    responses: ResponsesFixture, caplog
):
    entered, revoked, release_stale = asyncio.Event(), asyncio.Event(), asyncio.Event()
    other_entered, other_release = asyncio.Event(), asyncio.Event()
    notices, returned = [], []
    peer = Peer(names=("find-record",), label="allowed-survivor-receipt")

    async def approval(context, call):
        notices.append((context, call))
        if len(notices) == 1:
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                # Application cleanup can return a late value after native
                # revocation. It has no authority to request another stop.
                revoked.set()
                await release_stale.wait()
                returned.append("stale StopTurn")
                return StopTurn(PRIVATE_REASON)
        if len(notices) == 2:
            other_entered.set()
            await other_release.wait()
        return True

    responses.respond(
        code_call(
            'await tools.mcp__vault__find_record({key:"old"}); text("OLD-CONTINUED");', "old-cell"
        )
    )
    responses.respond(
        code_call('text(await tools.mcp__vault__find_record({key:"other"}));', "other-cell")
    )
    async with (
        timeout(45),
        peer.serve() as url,
        aclosing(
            NativeProvider(
                base_url=responses.url, api_key="synthetic-stale-mcp", supports_websockets=False
            )
        ) as provider,
    ):
        agent = Agent(
            instructions="Use the selected MCP.",
            tools=[
                HttpMCP(
                    name="vault",
                    url=url,
                    tools=["find-record"],
                    approval=approval,
                )
            ],
            provider=provider,
            programmatic_tool_calling=True,
        )
        async with agent.session() as session, agent.session() as other:
            old = await session.submit("old")
            leaving_waiter = asyncio.create_task(old.get_result())
            other_waiter = owner_waiter = None
            stopping = None
            try:
                await entered.wait()
                [(old_context, old_call)] = notices
                assert old_context.submission is old
                assert old_call.thread_id == session.id and old_call.turn_id == old.turn_id
                assert old_call.outer_call_id == "old-cell"
                assert old_call.cell_id and old_call.runtime_tool_call_id
                assert peer.calls == []
                other_turn = await other.submit("other")
                other_waiter = asyncio.create_task(other_turn.get_result())
                await other_entered.wait()
                other_context, other_call = notices[1]
                assert other_context.submission is other_turn
                assert other_call.thread_id == other.id and other_call.turn_id == other_turn.turn_id
                assert other_call.outer_call_id == "other-cell"
                assert other_call.thread_id != old_call.thread_id
                assert peer.calls == [] and len(responses.requests) == 2

                stopping = asyncio.create_task(old.cancel(mode="force"))
                await revoked.wait()
                assert session._root_interrupts and returned == []
                assert not stopping.done()
                assert not leaving_waiter.done() and not other_waiter.done()
                leaving_waiter.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await leaving_waiter
                owner_waiter = asyncio.create_task(old.get_result())
                assert not owner_waiter.done()
                release_stale.set()
                with pytest.raises(TurnStoppedError):
                    await owner_waiter
                await stopping
                assert returned == ["stale StopTurn"]
                assert peer.calls == [] and len(responses.requests) == 2
                assert not other_waiter.done() and not other_release.is_set()
                assert not session._root_interrupts

                responses.respond(
                    code_call(
                        'text(await tools.mcp__vault__find_record({key:"newer"}));', "newer-cell"
                    )
                )
                responses.respond(assistant_output("newer-survived"))
                newer = await session.submit("successor")
                assert newer is not old and newer.turn_id != old.turn_id
                assert (await newer.get_result()).final_output == "newer-survived"
                assert notices[2][0].submission is newer
                assert notices[2][1].outer_call_id == "newer-cell"
                assert [(c["name"], c["arguments"]) for c in peer.calls] == [
                    ("find-record", {"key": "newer"})
                ]
                assert not other_waiter.done()
                with pytest.raises(TurnStoppedError):
                    await old.get_result()
                assert (await newer.get_result()).final_output == "newer-survived"

                responses.respond(assistant_output("other-survived"))
                other_release.set()
                assert (await other_waiter).final_output == "other-survived"
                assert [(c["name"], c["arguments"]) for c in peer.calls] == [
                    ("find-record", {"key": "newer"}),
                    ("find-record", {"key": "other"}),
                ]
                assert len(notices) == 3 and len(responses.requests) == 5
                assert (await other_turn.get_result()).final_output == "other-survived"
                history = [item async for item in session.items()]
                [old_output] = [
                    item
                    for item in history
                    if item.get("type") == "function_call_output"
                    and item.get("call_id") == "old-cell"
                ]
                assert "OLD-CONTINUED" not in str(old_output["output"])
                assert (
                    PRIVATE_REASON
                    not in str(history) + json.dumps(responses.requests) + caplog.text
                )
            finally:
                release_stale.set()
                other_release.set()
                await asyncio.gather(
                    *(
                        task
                        for task in (leaving_waiter, owner_waiter, other_waiter)
                        if task is not None
                    ),
                    return_exceptions=True,
                )


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("kind", ["exec_command", "apply_patch", "write_stdin"])
@pytest.mark.parametrize("stop", [False, True], ids=["negative-approval", "stop-turn"])
async def test_executor_code_before_decision_stops_without_selected_or_following_effect(
    responses: ResponsesFixture, tmp_path, monkeypatch, kind, stop
):
    notices, phases = [], []
    baseline_writes = []

    async def approval(context, value):
        phases.append(("approval", context))
        if context.tool_name != kind:
            return True
        notices.append((context, value))
        return StopTurn(PRIVATE_REASON) if stop else False

    async def transform(context, value):
        phases.append(("before", context))
        return ReplaceArguments(value)

    async with (
        timeout(65),
        ExecutorPeer(tmp_path / "remote").serve() as peer,
        aclosing(
            NativeProvider(
                base_url=responses.url, api_key="synthetic-policy", supports_websockets=False
            )
        ) as provider,
    ):
        trust(peer, tmp_path, monkeypatch)
        if kind == "write_stdin":
            responses.respond(
                function_call(
                    "exec_command",
                    {
                        "cmd": "read -r value; printf '%s' \"$value\" >> selected-effect",
                        "tty": True,
                        "yield_time_ms": 1000,
                    },
                    "start",
                )
            )
            responses.respond(code_call("", "stdin-cell"))

            async def actual_process(event):
                item = event.get("item", {})
                if item.get("call_id") != "stdin-cell":
                    return
                output = tool_output(responses.requests[-1], "start")
                match = re.search(r"Process running with session ID (\d+)", output)
                assert match, output
                baseline_writes.append(
                    sum(r.get("method") == "process/write" for r in peer.requests)
                )
                args = {"session_id": int(match[1]), "chars": "forbidden\n", "yield_time_ms": 1000}
                item["input"] = (
                    "try { await tools.write_stdin(" + json.dumps(args) + "); } catch (denial) {}"
                    'await tools.exec_command({cmd:"printf follow >> after-effect"});'
                    'text("AFTER-SELECTED");'
                )

            responses.before_event = actual_process
        else:
            args = (
                {"cmd": "printf forbidden >> selected-effect"}
                if kind == "exec_command"
                else "*** Begin Patch\n*** Add File: selected-effect\n+forbidden\n*** End Patch"
            )
            # Use an ordinary Python receipt for the next effect so it cannot
            # be intercepted by the same selected executor approval.
            responses.respond(
                code_call(
                    "try { await tools." + kind + "(" + json.dumps(args) + "); } catch (denial) {}"
                    'await tools.after_selected({}); text("AFTER-SELECTED");',
                    "selected-cell",
                )
            )
        after = []

        @function_tool
        def after_selected() -> str:
            after.append("executed")
            return "after-receipt"

        responses.respond(assistant_output("continued"))
        async with Agent(
            instructions="Use tools.",
            provider=provider,
            tools=[after_selected],
            programmatic_tool_calling=True,
        ).session(
            environment=declaration(peer, approval=approval, transform_arguments=transform)
        ) as session:
            turn = await session.submit("try selected")
            with pytest.raises(TurnStoppedError, match="^Turn stopped by policy$") as caught:
                await turn.get_result()
            assert PRIVATE_REASON not in str(caught.value)
            assert PRIVATE_REASON not in str(caught.value.__context__) + str(caught.value.__cause__)
            [(context, value)] = notices
            assert context.submission is turn
            assert context.session is session and context.thread_id == session.id
            assert context.turn_id == turn.turn_id
            assert context.outer_call_id == (
                "stdin-cell" if kind == "write_stdin" else "selected-cell"
            )
            assert context.cell_id and context.runtime_tool_call_id
            assert not (peer.root / "selected-effect").exists()
            selected_phases = [(phase, ctx) for phase, ctx in phases if ctx.tool_name == kind]
            assert [phase for phase, _ in selected_phases] == ["approval"]
            assert all(ctx is context for _, ctx in selected_phases)
            before_requests = 2 if kind == "write_stdin" else 1
            assert len(responses.requests) == before_requests
            if kind == "write_stdin":
                assert [
                    sum(r.get("method") == "process/write" for r in peer.requests)
                ] == baseline_writes
                assert not (peer.root / "after-effect").exists()
            else:
                assert after == []
            items = [item async for item in session.items()]
            assert PRIVATE_REASON not in str(items) + json.dumps(responses.requests)
            assert "AFTER-SELECTED" not in str(
                [item for item in items if item.get("type") == "function_call_output"]
            )
