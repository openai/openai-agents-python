"""Public native cells keep their issuing executor owner across root lifetimes."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

import pytest

from agents import Agent, HttpMCP, NativeError, ReplaceArguments, StopTurn, ToolRef
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_child_agents import trust
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_policies import OutputEvents
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_tools import Peer

PRIVATE = "yielded-owner-private-f618"
UNACCEPTED = "yielded-owner-unaccepted-4701"
OUTER = "original-displayed-call"


def output_for(responses, call_id, *, code=False):
    kind = "custom_tool_call_output" if code else "function_call_output"
    request = next(
        request
        for request in reversed(responses.requests)
        if any(
            item.get("type") == kind and item.get("call_id") == call_id
            for item in request.get("input", [])
        )
    )
    return code_output(request, call_id) if code else tool_output(request, call_id)


@asynccontextmanager
async def yielded_cell(
    responses,
    tmp_path,
    monkeypatch,
    *,
    approval=None,
    arguments=None,
    result=None,
    policy_timeout=25,
    gated=True,
    following=False,
    capture_events=True,
):
    gate = Peer(names=("release",), release_call=asyncio.Event())
    async with gate.serve() as url, ExecutorPeer(tmp_path / "remote").serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        (peer.root / "private.txt").write_text(PRIVATE)
        command = "cat private.txt; printf once >> completed-effect"
        script = "yield_control(); "
        if gated:
            script += 'await tools.mcp__gate__release({key:"owner-gate"}); '
        invocation = (
            "const value = await tools.exec_command("
            + json.dumps({"cmd": command, "yield_time_ms": 30000})
            + "); text(value.output);"
        )
        if following:
            script += "try { " + invocation + " } catch (denied) {} "
            script += 'await tools.exec_command({cmd:"printf forbidden > following-effect"});'
            script += 'text("FORBIDDEN-CONTINUATION");'
        else:
            script += invocation
        responses.respond(code_call(script, OUTER))
        responses.respond(assistant_output("original cached completion"))
        memory = object()
        agent = Agent(
            instructions="Keep the original cell owner.",
            model="gpt-5.5",
            tools=[HttpMCP(name="gate", url=url, tools=["release"], approval_mode="approve")],
            provider=provider(responses),
            programmatic_tool_calling=True,
        )
        async with AsyncExitStack() as stack:
            session = await stack.enter_async_context(
                agent.session(
                    context=memory,
                    environment=declaration(
                        peer,
                        approval=approval,
                        transform_arguments=arguments,
                        transform_result=result,
                        policy_timeout=policy_timeout,
                    ),
                )
            )
            captured = None
            if capture_events:
                captured = OutputEvents(session._native)
                session._native = captured
            first = await session.submit("Start the original admitted cell")
            run = SimpleNamespace(
                session=session,
                first=first,
                peer=peer,
                gate=gate,
                captured=captured,
                memory=memory,
                agent=agent,
                stack=stack,
            )
            try:
                yield run
            finally:
                gate.release_call.set()


async def finish_and_compact(run, responses):
    assert (await run.first.get_result()).final_output == "original cached completion"
    await run.gate.call_entered.wait()
    receipt = output_for(responses, OUTER, code=True)
    prefix = "Script running with cell ID "
    assert receipt.startswith(prefix), receipt
    run.cell = receipt.removeprefix(prefix).split()[0].rstrip(".")
    responses.respond(assistant_output("Native summary without the original tool call."))
    await run.session.compact()
    await run.session.update(model="gpt-5.2")


def original_context(run, context):
    assert context.submission is run.first and context.session is run.session
    assert context.agent is run.agent and context.context is run.memory
    assert (context.thread_id, context.turn_id) == (run.session.id, run.first.turn_id)
    assert context.outer_call_id == OUTER and context.cell_id == run.cell
    assert context.runtime_tool_call_id and context.call_id is None
    assert context.model == "gpt-5.5" and context.agent_role is None


async def start_wait(run, responses, *, terminate=False, expect_pending=False):
    sent = asyncio.Event()
    previous = responses.before_event

    async def observe(event):
        if previous is not None:
            await previous(event)
        if event.get("type") == "response.completed":
            # This is a public requested-wait barrier, not proof of the private
            # Cell.operation lock; the native regression owns that proof.
            asyncio.get_running_loop().call_soon(sent.set)

    responses.before_event = observe
    responses.respond(
        function_call(
            "wait",
            {
                "cell_id": run.cell,
                "yield_time_ms": 10000,
                **({"terminate": True} if terminate else {}),
            },
            "collect-old",
        )
    )
    responses.respond(assistant_output("successor survived"))
    later = await run.session.submit(
        "Observe only the old cell", tools=[ToolRef("wait", "functions")]
    )
    pending = asyncio.create_task(later.get_result())
    await sent.wait()
    if expect_pending:
        assert not pending.done()
    return later, pending


async def no_private_output(run, responses):
    history = json.loads(await run.captured.read_thread_history(None))
    wire = json.dumps([history, run.captured.events, responses.requests])
    assert PRIVATE not in wire and UNACCEPTED not in wire
    assert (await run.first.get_result()).final_output == "original cached completion"


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"),
    reason="requires Linux or macOS executor write policies",
)
async def test_writable_roots_cannot_change_while_a_yielded_cell_owns_them(
    responses, tmp_path, monkeypatch
):
    old_root = tmp_path / "old-root"
    old_root.mkdir()
    gate = Peer(names=("release",), release_call=asyncio.Event())
    executor = ExecutorPeer(tmp_path / "remote", synthetic_process_output="late command complete")
    async with timeout(35), gate.serve() as url, executor.serve() as peer:
        trust(peer, tmp_path, monkeypatch)
        agent = Agent(
            instructions="Use the admitted code cell.",
            tools=[HttpMCP(name="gate", url=url, tools=["release"], approval_mode="approve")],
            provider=provider(responses),
            programmatic_tool_calling=True,
        )
        environment = declaration(peer, writable_roots=[old_root])
        script = (
            'yield_control(); await tools.mcp__gate__release({key:"root-gate"}); '
            'const result = await tools.exec_command({cmd:"printf late"}); '
            "text(result.output);"
        )
        responses.respond(code_call(script, OUTER))
        responses.respond(assistant_output("original turn finished"))
        async with agent.session(environment=environment) as session:
            try:
                first = await session.submit("Start a cell with the original roots")
                await gate.call_entered.wait()
                assert (await first.get_result()).final_output == "original turn finished"
                assert session.active_turn is None
                receipt = output_for(responses, OUTER, code=True)
                prefix = "Script running with cell ID "
                assert receipt.startswith(prefix), receipt
                cell = receipt.removeprefix(prefix).split()[0].rstrip(".")
                assert not peer.synthetic_process_starts

                before = len(responses.requests)
                responses.respond(assistant_output("same roots accepted"))
                refused = await session.submit("Narrow the roots", writable_roots=[])
                refusal = (
                    "macOS executors require fixed writable roots"
                    if sys.platform == "darwin"
                    else "old executor work"
                )
                with pytest.raises(NativeError, match=refusal) as failure:
                    await refused.get_result()
                assert failure.value.effects == "not_started"
                assert len(responses.requests) == before

                same = await session.submit("Keep the roots", writable_roots=[old_root])
                assert (await same.get_result()).final_output == "same roots accepted"
                assert not peer.synthetic_process_starts

                responses.respond(
                    function_call("wait", {"cell_id": cell, "yield_time_ms": 10000}, "collect-cell")
                )
                responses.respond(assistant_output("cell collected"))
                gate.release_call.set()
                collector = await session.submit("Collect the old cell")
                assert (await collector.get_result()).final_output == "cell collected"
                assert "late command complete" in output_for(responses, "collect-cell")
                assert len(peer.synthetic_process_starts) == 1

                before = len(responses.requests)
                responses.respond(assistant_output("narrower roots accepted"))
                narrower = await session.submit("Now narrow the roots", writable_roots=[])
                if sys.platform == "darwin":
                    with pytest.raises(NativeError, match=refusal) as failure:
                        await narrower.get_result()
                    assert failure.value.effects == "not_started"
                    assert len(responses.requests) == before
                else:
                    assert (await narrower.get_result()).final_output == "narrower roots accepted"
            finally:
                gate.release_call.set()


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("delivery", ["idle", "during-successor", "after-successor"])
async def test_first_executor_call_after_root_completion_uses_original_cell_owner(
    responses, tmp_path, monkeypatch, delivery
):
    contexts, arguments_contexts, approvals = [], [], []
    entered, release, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()

    def approve(ctx, value):
        approvals.append(ctx)
        return True

    def arguments(ctx, value):
        arguments_contexts.append(ctx)
        return ReplaceArguments(value)

    async def result(ctx, value):
        contexts.append(ctx)
        assert PRIVATE in json.dumps(value)
        entered.set()
        await release.wait()
        returned.set()
        return {"output": "accepted-original-owner"}

    async with (
        timeout(65),
        yielded_cell(
            responses, tmp_path, monkeypatch, approval=approve, arguments=arguments, result=result
        ) as run,
    ):
        try:
            await finish_and_compact(run, responses)
            assert not approvals and not arguments_contexts and not contexts
            assert not any(req.get("method") == "process/start" for req in run.peer.requests)
            later = pending = None
            if delivery == "during-successor":
                later, pending = await start_wait(run, responses, expect_pending=True)
            elif delivery == "after-successor":
                responses.respond(assistant_output("narrowed successor already completed"))
                prior = await run.session.submit("No tools", tools=[])
                assert (
                    await prior.get_result()
                ).final_output == "narrowed successor already completed"
            assert (
                run.session.active_turn is None
                if delivery != "during-successor"
                else run.session.active_turn is later
            )
            run.gate.release_call.set()
            await entered.wait()
            original_context(run, contexts[0])
            assert approvals[0] is arguments_contexts[0]
            for context in approvals + arguments_contexts:
                original_context(run, context)
                assert context.runtime_tool_call_id == contexts[0].runtime_tool_call_id
            release.set()
            await returned.wait()
            if pending is None:
                later, pending = await start_wait(run, responses)
            assert (await pending).final_output == "successor survived"
            assert later is not run.first
            assert "accepted-original-owner" in output_for(responses, "collect-old")
            assert len(contexts) == len(approvals) == len(arguments_contexts) == 1
            assert (run.peer.root / "completed-effect").read_text() == "once"
            assert sum(req.get("method") == "process/start" for req in run.peer.requests) == 1
            assert responses.requests[-1]["model"] == "gpt-5.2"
            new_requests = [
                request for request in responses.requests if request["model"] == "gpt-5.2"
            ]
            assert new_requests and all(
                not any(item.get("call_id") == OUTER for item in request["input"])
                for request in new_requests
            )
            await no_private_output(run, responses)
        finally:
            release.set()
            run.gate.release_call.set()


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("stop", [False, True], ids=["false", "stop-turn"])
async def test_late_original_cell_stop_does_not_cancel_passive_successor(
    responses, tmp_path, monkeypatch, stop
):
    entered, decide = asyncio.Event(), asyncio.Event()
    contexts = []

    async def approval(ctx, value):
        contexts.append(ctx)
        entered.set()
        await decide.wait()
        return StopTurn("private-old-cell-stop") if stop else False

    async with (
        timeout(65),
        yielded_cell(responses, tmp_path, monkeypatch, approval=approval, following=True) as run,
    ):
        try:
            await finish_and_compact(run, responses)
            assert not contexts
            later, pending = await start_wait(run, responses, expect_pending=True)
            run.gate.release_call.set()
            await entered.wait()
            original_context(run, contexts[0])
            assert not pending.done()
            decide.set()
            async with timeout(8):
                assert (await pending).final_output == "successor survived"
            assert later is not run.first and len(contexts) == 1
            assert not (run.peer.root / "completed-effect").exists()
            assert not (run.peer.root / "following-effect").exists()
            assert not any(req.get("method") == "process/start" for req in run.peer.requests)
            assert "private-old-cell-stop" not in json.dumps(responses.requests)
            assert "FORBIDDEN-CONTINUATION" not in output_for(responses, "collect-old")
            await no_private_output(run, responses)
        finally:
            decide.set()
            run.gate.release_call.set()


@pytest.mark.requires_native_linux_sandbox
async def test_result_callback_survives_original_normal_terminal_after_entry(
    responses, tmp_path, monkeypatch
):
    entered, release, returned, cancelled = (asyncio.Event() for _ in range(4))
    contexts = []

    async def result(ctx, value):
        contexts.append(ctx)
        assert PRIVATE in json.dumps(value)
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        returned.set()
        return {"output": "accepted-across-normal-terminal"}

    async def before_event(event):
        if event.get("item", {}).get("type") == "message":
            await entered.wait()

    responses.before_event = before_event
    async with (
        timeout(65),
        yielded_cell(responses, tmp_path, monkeypatch, result=result, gated=False) as run,
    ):
        try:
            assert (await run.first.get_result()).final_output == "original cached completion"
            assert entered.is_set() and not cancelled.is_set() and not returned.is_set()
            receipt = output_for(responses, OUTER, code=True)
            run.cell = receipt.removeprefix("Script running with cell ID ").split()[0].rstrip(".")
            original_context(run, contexts[0])
            release.set()
            await returned.wait()
            later, pending = await start_wait(run, responses)
            assert (await pending).final_output == "successor survived"
            assert later is not run.first and not cancelled.is_set() and len(contexts) == 1
            assert "accepted-across-normal-terminal" in output_for(responses, "collect-old")
            await no_private_output(run, responses)
        finally:
            release.set()


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("cleanup", ["terminate", "timeout", "close", "cancel-all"])
async def test_late_result_cleanup_joins_original_callback_without_publication(
    responses, tmp_path, monkeypatch, cleanup
):
    entered, cancelled, release, joined = (asyncio.Event() for _ in range(4))
    contexts = []

    async def result(ctx, value):
        contexts.append(ctx)
        assert PRIVATE in json.dumps(value)
        entered.set()
        try:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return {"output": UNACCEPTED}
        finally:
            joined.set()

    async with (
        timeout(65),
        yielded_cell(
            responses,
            tmp_path,
            monkeypatch,
            result=result,
            policy_timeout=0.25 if cleanup == "timeout" else 25,
            capture_events=cleanup != "cancel-all",
        ) as run,
    ):
        closing = None
        try:
            await finish_and_compact(run, responses)
            assert not contexts
            run.gate.release_call.set()
            await entered.wait()
            original_context(run, contexts[0])
            if cleanup == "cancel-all":
                assert run.session.active_turn is None
                closing = asyncio.create_task(run.session.cancel_all())
                cancel_seen = asyncio.create_task(cancelled.wait())
                try:
                    # Shorter than the 25-second policy deadline: native graph
                    # cancellation, not eventual timeout, must cancel this work.
                    async with timeout(8):
                        await asyncio.wait(
                            (closing, cancel_seen), return_when=asyncio.FIRST_COMPLETED
                        )
                    assert cancelled.is_set(), (
                        "cancel_all returned without cancelling the old callback"
                    )
                    assert not closing.done() and not joined.is_set()
                    release.set()
                    await closing
                    assert joined.is_set()
                finally:
                    cancel_seen.cancel()
                    await asyncio.gather(cancel_seen, return_exceptions=True)
                responses.respond(assistant_output("reused after graph cancellation"))
                successor = await run.session.submit("Continue without tools", tools=[])
                assert (
                    await successor.get_result()
                ).final_output == "reused after graph cancellation"
                assert successor is not run.first
                items = [item async for item in run.session.items()]
                public_output = json.dumps([items, responses.requests])
                assert PRIVATE not in public_output and UNACCEPTED not in public_output
            elif cleanup == "close":
                closing = asyncio.create_task(run.stack.aclose())
                await cancelled.wait()
                assert not closing.done() and not joined.is_set()
                release.set()
                await closing
                assert joined.is_set()
                assert PRIVATE not in json.dumps([run.captured.events, responses.requests])
                assert UNACCEPTED not in json.dumps([run.captured.events, responses.requests])
            else:
                later, pending = await start_wait(run, responses, terminate=cleanup == "terminate")
                await cancelled.wait()
                release.set()
                await joined.wait()
                assert (await pending).final_output == "successor survived"
                assert later is not run.first
                assert UNACCEPTED not in output_for(responses, "collect-old")
                await no_private_output(run, responses)
            assert len(contexts) == 1
            assert (await run.first.get_result()).final_output == "original cached completion"
            assert (run.peer.root / "completed-effect").read_text() == "once"
            assert sum(req.get("method") == "process/start" for req in run.peer.requests) == 1
        finally:
            release.set()
            run.gate.release_call.set()
            if closing is not None:
                await closing
