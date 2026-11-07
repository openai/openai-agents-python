"""Native result deadlines fence their issuing owner before any continuation."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext

import pytest

from agents import Agent, HttpMCP, NativeError
from agents.session import Session
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider
from tests.test_native_child_agents import trust
from tests.test_native_executor_policies import OutputEvents
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_tools import Peer, mcp_call
from tests.test_native_yielded_executor_owner import (
    PRIVATE,
    UNACCEPTED,
    finish_and_compact,
    no_private_output,
    start_wait,
    yielded_cell,
)


@pytest.mark.parametrize(
    "family", [pytest.param("executor", marks=pytest.mark.requires_native_linux_sandbox), "mcp"]
)
@pytest.mark.parametrize("terminal_only", [False, True])
async def test_queued_native_result_expiry_stops_before_next_model_request(
    responses, tmp_path, monkeypatch, family, terminal_only
):
    queued = asyncio.Event()
    entered = []

    async def policy(context, value):
        entered.append(context)
        return value

    executor_policy = Session._invoke_executor_policy
    mcp_policy = Session._invoke_mcp_policy

    async def defer_executor(self, state, nonce, request, **kwargs):
        if request["phase"] == "after":
            queued.set()
            await asyncio.Future()
        await executor_policy(self, state, nonce, request, **kwargs)

    async def defer_mcp(self, state, notice):
        queued.set()
        await asyncio.Future()
        await mcp_policy(self, state, notice)

    monkeypatch.setattr(Session, "_invoke_executor_policy", defer_executor)
    monkeypatch.setattr(Session, "_invoke_mcp_policy", defer_mcp)

    class TerminalOnlyEvents(OutputEvents):
        async def _next_session_event(self):
            while True:
                raw = await super()._next_session_event()
                event = json.loads(raw)
                if not (
                    event["msg"]["type"] == "_sdk_model_generation_failed"
                    and event.get("subagent_id") is None
                    and event["msg"].get("policy_stop") is True
                ):
                    return raw

    peer = Peer(label="queued-private-native-result-731b")
    executor_scope = (
        ExecutorPeer(tmp_path / "remote").serve() if family == "executor" else nullcontext()
    )
    async with timeout(45), peer.serve() as url, executor_scope as executor:
        if executor is not None:
            trust(executor, tmp_path, monkeypatch)
            (executor.root / "private.txt").write_text(peer.label)
        tools = []
        environment = None
        if family == "executor":
            assert executor is not None
            environment = declaration(executor, transform_result=policy, policy_timeout=0.1)
            responses.respond(
                function_call(
                    "exec_command", {"cmd": "cat private.txt; printf once >> effect"}, "expires"
                )
            )
        else:
            tools = [
                HttpMCP(
                    name="vault",
                    url=url,
                    tools=["search"],
                    transform_result=policy,
                    transform_result_timeout=0.1,
                )
            ]
            responses.respond(mcp_call("search", "mcp__vault", "expires"))
        async with Agent(
            instructions="Read once.", tools=tools, provider=provider(responses)
        ).session(environment=environment) as session:
            # Native completion can retire the early policy notice. Exercise
            # settlement through the real terminal event alone as well.
            captured = (TerminalOnlyEvents if terminal_only else OutputEvents)(session._native)
            session._native = captured
            turn = await session.submit("Read the private result")
            await queued.wait()
            expected = (
                "Executor policy failed"
                if family == "executor"
                else "Native MCP result policy failed"
            )
            with pytest.raises(NativeError, match=expected) as failure:
                await turn.get_result()
            if terminal_only and family == "executor":
                terminal = next(
                    event["msg"]
                    for event in captured.events
                    if event["id"] == turn.turn_id
                    and event["msg"]["type"] in {"task_complete", "turn_complete"}
                )
                assert terminal["_sdk_policy_failure"] == expected
            assert failure.value.__cause__ is None and failure.value.__context__ is None
            assert entered == []
            assert len(responses.requests) == 1
            if family == "executor":
                assert executor is not None
                assert (executor.root / "effect").read_text() == "once"
            else:
                assert len(peer.calls) == 1
            responses.respond(assistant_output("successor survived"))
            successor = await session.submit("Continue without tools", tools=[])
            assert (await successor.get_result()).final_output == "successor survived"
            history = json.loads(await captured.read_thread_history(None))
            assert peer.label not in json.dumps([history, captured.events, responses.requests])


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("delivered", [False, True])
async def test_retained_result_expiry_stops_original_cell_and_preserves_successor(
    responses, tmp_path, monkeypatch, delivered
):
    queued, cancelled, exited = (asyncio.Event() for _ in range(3))
    invoked = []
    original = Session._invoke_executor_policy

    async def policy(context, value):
        invoked.append(context)
        queued.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            return {"output": UNACCEPTED}
        finally:
            exited.set()

    async def defer(self, state, nonce, request, **kwargs):
        if request["phase"] == "after" and not delivered:
            queued.set()
            try:
                await asyncio.Future()
            finally:
                exited.set()
        await original(self, state, nonce, request, **kwargs)

    monkeypatch.setattr(Session, "_invoke_executor_policy", defer)
    async with (
        timeout(60),
        yielded_cell(
            responses,
            tmp_path,
            monkeypatch,
            result=policy,
            policy_timeout=0.3,
            following=True,
        ) as run,
    ):
        await finish_and_compact(run, responses)
        run.gate.release_call.set()
        await queued.wait()
        later, pending = await start_wait(run, responses)
        assert (await pending).final_output == "successor survived"
        await exited.wait()
        assert later is not run.first
        assert not (run.peer.root / "following-effect").exists()
        assert (run.peer.root / "completed-effect").read_text() == "once"
        assert len(invoked) == int(delivered)
        if delivered:
            assert cancelled.is_set()
            assert invoked[0].submission is run.first
        await no_private_output(run, responses)
        assert PRIVATE not in json.dumps(responses.requests)


async def test_retained_mcp_result_expiry_stops_old_cell_without_failing_successor(responses):
    from types import SimpleNamespace

    from tests.test_native_code_mode import code_call

    gate = Peer(names=("release",), release_call=asyncio.Event())
    selected = Peer(label=PRIVATE, release_call=asyncio.Event())
    following = Peer(label="forbidden-mcp-continuation")
    invoked = []

    async def policy(context, value):
        invoked.append(context)
        return value

    async with (
        timeout(45),
        gate.serve() as gate_url,
        selected.serve() as selected_url,
        following.serve() as following_url,
    ):
        responses.respond(
            code_call(
                'yield_control(); await tools.mcp__gate__release({key:"gate"}); '
                'try { await tools.mcp__vault__search({key:"private"}); } catch (denied) {} '
                'await tools.mcp__following__search({key:"forbidden"}); text("continued");',
                "original-displayed-call",
            )
        )
        responses.respond(assistant_output("original cached completion"))
        async with Agent(
            instructions="Keep the original cell owner.",
            tools=[
                HttpMCP(name="gate", url=gate_url, tools=["release"], approval_mode="preapproved"),
                HttpMCP(
                    name="vault",
                    url=selected_url,
                    tools=["search"],
                    transform_result=policy,
                    transform_result_timeout=0.3,
                ),
                HttpMCP(name="following", url=following_url, tools=["search"]),
            ],
            provider=provider(responses),
            programmatic_tool_calling=True,
        ).session() as session:
            captured = OutputEvents(session._native)
            session._native = captured
            first = await session.submit("Start background work")
            run = SimpleNamespace(session=session, first=first, gate=gate, captured=captured)
            try:
                await finish_and_compact(run, responses)
                gate.release_call.set()
                await selected.call_entered.wait()
                later, pending = await start_wait(run, responses)
                # The old result must retain its policy after the successor narrows tools.
                selected.release_call.set()
                assert (await pending).final_output == "successor survived"
                assert later is not first and invoked == []
                assert len(selected.calls) == 1 and following.calls == []
                await no_private_output(run, responses)
            finally:
                selected.release_call.set()
                gate.release_call.set()
