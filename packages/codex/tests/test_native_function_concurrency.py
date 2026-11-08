"""A native owner bounds automatic callbacks without serializing manual work."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import ClassVar

import pytest
from pydantic import BaseModel, model_validator

from agents import (
    Agent,
    Allow,
    FunctionTool,
    NativeAgents,
    ToolContext,
    ToolOutput,
    TurnStoppedError,
    function_tool,
)
from agents.observation import ToolStarted
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_manual_tools import LookupArgs, next_request


class Payload(BaseModel):
    value: int
    validated: ClassVar[list[int]] = []

    @model_validator(mode="after")
    def record(self):
        self.validated.append(self.value)
        return self


async def queued(session, count):
    # Arrange an actual queued dispatch, rather than guessing from an elapsed sleep.
    while session._active is None or len(session._active.callbacks) < count:
        await asyncio.sleep(0)


def calls(count=3):
    return [function_call("work", {"payload": {"value": i}}, f"call-{i}") for i in range(count)]


@pytest.mark.parametrize("limit", [None, 1, 2])
async def test_limit_precedes_validators_and_hooks_and_releases_for_all_calls(responses, limit):
    Payload.validated = []
    entered, before = [], []
    release = asyncio.Event()
    admitted = asyncio.Event()
    expected = limit or 3
    active = peak = 0

    async def check(ctx):
        before.append(ctx.original_arguments["payload"]["value"])
        return Allow()

    @function_tool(before=check)
    async def work(payload: Payload) -> str:
        nonlocal active, peak
        entered.append(payload.value)
        active += 1
        peak = max(peak, active)
        if len(entered) == expected:
            admitted.set()
        try:
            await release.wait()
            await asyncio.sleep(0)
            return str(payload.value)
        finally:
            active -= 1

    responses.respond(*calls())
    responses.respond(assistant_output("all settled"))
    async with (
        timeout(15),
        Agent(instructions="work", tools=[work], provider=provider(responses)).session(
            max_function_concurrency=limit
        ) as session,
    ):
        turn = await session.submit("work")
        try:
            await admitted.wait()
            await queued(session, 3)
            assert len(entered) == len(before) == len(Payload.validated) == expected
        finally:
            release.set()
        assert (await turn.get_result()).final_output == "all settled"
        assert peak == expected and active == 0
        assert sorted(entered) == sorted(before) == sorted(Payload.validated) == [0, 1, 2]
        assert {tool_output(responses.requests[1], f"call-{i}") for i in range(3)} == {
            "0",
            "1",
            "2",
        }


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "2"])
async def test_invalid_limit_rejected_before_native_open(responses, limit):
    agent = Agent(instructions="test", provider=provider(responses))
    with pytest.raises(ValueError, match="positive integer"):
        async with agent.session(max_function_concurrency=limit):
            pytest.fail("invalid limit was admitted")
    assert responses.requests == []


async def test_fatal_failure_seals_admission_before_waking_waiter_and_next_turn_recovers(responses):
    entered = []
    fail = asyncio.Event()
    first = asyncio.Event()
    broken = True

    @function_tool
    async def work(payload: Payload) -> str:
        entered.append(payload.value)
        first.set()
        if broken:
            await fail.wait()
            raise ValueError("callback failed")
        return str(payload.value)

    Payload.validated = []
    responses.respond(*calls())
    async with (
        timeout(15),
        Agent(instructions="work", tools=[work], provider=provider(responses)).session(
            max_function_concurrency=1
        ) as session,
    ):
        turn = await session.submit("fail")
        await first.wait()
        await queued(session, 3)
        fail.set()
        with pytest.raises(ValueError, match="callback failed"):
            await turn.get_result()
        assert len(entered) == len(Payload.validated) == 1
        assert len([event async for event in turn.events() if isinstance(event, ToolStarted)]) == 1
        broken = False
        entered.clear()
        Payload.validated.clear()
        responses.respond(*calls())
        responses.respond(assistant_output("recovered"))
        assert (await (await session.submit("retry")).get_result()).final_output == "recovered"
        assert sorted(entered) == [0, 1, 2]


async def test_cancel_joins_entered_cleanup_and_never_starts_queued_work(responses):
    entered, cleaned = [], []
    first = asyncio.Event()
    cleanup = asyncio.Event()
    release_cleanup = asyncio.Event()

    @function_tool
    async def work(payload: Payload) -> str:
        entered.append(payload.value)
        first.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release_cleanup.wait()
            cleaned.append(payload.value)
        return "unreachable"

    Payload.validated = []
    responses.respond(*calls())
    async with (
        timeout(15),
        Agent(instructions="work", tools=[work], provider=provider(responses)).session(
            max_function_concurrency=1
        ) as session,
    ):
        turn = await session.submit("cancel")
        await first.wait()
        await queued(session, 3)
        cancelling = asyncio.create_task(turn.cancel())
        try:
            await cleanup.wait()
            assert not cancelling.done()
            assert len(entered) == len(Payload.validated) == 1
        finally:
            release_cleanup.set()
        await cancelling
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        assert cleaned == entered
        assert len([event async for event in turn.events() if isinstance(event, ToolStarted)]) == 1
        responses.respond(assistant_output("next turn"))
        assert (await (await session.submit("continue")).get_result()).final_output == "next turn"
        assert len(entered) == 1


@pytest.mark.parametrize("kind", ["error_result", "timeout_result"])
async def test_failed_result_is_not_a_fatal_admission_error(responses, kind):
    entered = []

    @function_tool(callback_timeout=0.03, timeout_behavior="error_as_result")
    async def work(payload: Payload) -> ToolOutput:
        entered.append(payload.value)
        if payload.value == 0:
            if kind == "timeout_result":
                await asyncio.Event().wait()
            return ToolOutput.error("ordinary failure")
        return ToolOutput.text(str(payload.value))

    responses.respond(*calls())
    responses.respond(assistant_output("all settled"))
    async with (
        timeout(15),
        Agent(instructions="work", tools=[work], provider=provider(responses)).session(
            max_function_concurrency=1
        ) as session,
    ):
        assert (await (await session.submit("work")).get_result()).final_output == "all settled"
        assert sorted(entered) == [0, 1, 2]
        assert tool_output(responses.requests[1], "call-1") == "1"
        assert tool_output(responses.requests[1], "call-2") == "2"


async def test_manual_reply_does_not_wait_for_automatic_permit(responses):
    release = asyncio.Event()
    entered = []

    @function_tool
    async def work(payload: Payload) -> str:
        entered.append(payload.value)
        await release.wait()
        return str(payload.value)

    manual = FunctionTool(name="manual", description="", input_type=LookupArgs, output_type=str)
    responses.respond(*calls(2), function_call("manual", {"key": "manual"}, "manual-call"))
    responses.respond(assistant_output("complete"))
    async with (
        timeout(15),
        Agent(instructions="work", tools=[work, manual], provider=provider(responses)).session(
            max_function_concurrency=1
        ) as session,
    ):
        turn = await session.submit("work")
        try:
            request = await next_request(session.interactions(), manual)
            await queued(session, 2)
            await request.respond("manual accepted")
            assert len(entered) == 1
        finally:
            release.set()
        assert (await turn.get_result()).final_output == "complete"
        assert tool_output(responses.requests[1], "manual-call") == "manual accepted"


async def test_sync_worker_cleanup_holds_slot_and_turn_until_joined(responses):
    Payload.validated = []
    entered, cleanup = asyncio.Event(), asyncio.Event()
    release_body, release_cleanup = threading.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    effects = []

    @function_tool
    def work(payload: Payload) -> str:
        effects.append(payload.value)
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release_body.wait(10)
        finally:
            loop.call_soon_threadsafe(cleanup.set)
            assert release_cleanup.wait(10)
        return str(payload.value)

    responses.respond(*calls())
    async with (
        timeout(15),
        Agent(instructions="work", tools=[work], provider=provider(responses)).session(
            max_function_concurrency=1
        ) as session,
    ):
        turn = await session.submit("cancel worker")
        try:
            await entered.wait()
            await queued(session, 3)
            cancelling = asyncio.create_task(turn.cancel())
            while not session._active.stopping:
                await asyncio.sleep(0)
            release_body.set()
            await cleanup.wait()
            assert not cancelling.done()
            assert len(effects) == len(Payload.validated) == 1
            with pytest.raises(RuntimeError, match="finishing root interruption"):
                await session.submit("too early")
        finally:
            release_body.set()
            release_cleanup.set()
        await cancelling
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        assert len([event async for event in turn.events() if isinstance(event, ToolStarted)]) == 1
        responses.respond(*calls())
        responses.respond(assistant_output("reused"))
        assert (await (await session.submit("next turn")).get_result()).final_output == "reused"
        assert len(effects) == 4


async def test_timeout_formatter_keeps_permit_after_body_cleanup(responses):
    Payload.validated = []
    formatting, release = asyncio.Event(), asyncio.Event()
    entered = []

    async def format_timeout(ctx, error):
        formatting.set()
        await release.wait()
        return "joined timeout"

    @function_tool(
        callback_timeout=0.02,
        timeout_behavior="error_as_result",
        timeout_error_function=format_timeout,
    )
    async def work(payload: Payload) -> str:
        entered.append(payload.value)
        if len(entered) == 1:
            await asyncio.Event().wait()
        return str(payload.value)

    responses.respond(*calls())
    responses.respond(assistant_output("formatted"))
    async with (
        timeout(15),
        Agent(instructions="work", tools=[work], provider=provider(responses)).session(
            max_function_concurrency=1
        ) as session,
    ):
        turn = await session.submit("timeout")
        try:
            await formatting.wait()
            await queued(session, 3)
            assert entered == Payload.validated and len(entered) == 1
        finally:
            release.set()
        assert (await turn.get_result()).final_output == "formatted"
        assert sorted(entered) == [0, 1, 2]
        assert tool_output(responses.requests[1], f"call-{entered[0]}") == "joined timeout"


async def test_native_reply_settlement_keeps_permit(responses, monkeypatch):
    Payload.validated = []
    received, release = asyncio.Event(), asyncio.Event()
    entered = []

    @function_tool
    async def work(payload: Payload) -> str:
        entered.append(payload.value)
        return str(payload.value)

    responses.respond(*calls())
    responses.respond(assistant_output("acknowledged"))
    async with (
        timeout(15),
        Agent(instructions="work", tools=[work], provider=provider(responses)).session(
            max_function_concurrency=1
        ) as session,
    ):
        native = session._native

        class DelayedAck:
            def __getattr__(self, name):
                return getattr(native, name)

            async def _respond_python_tool(self, reply):
                accepted = await native._respond_python_tool(reply)
                if not received.is_set():
                    received.set()
                    await release.wait()
                return accepted

        monkeypatch.setattr(session, "_native", DelayedAck())
        turn = await session.submit("hold reply")
        try:
            await received.wait()
            await queued(session, 3)
            assert entered == Payload.validated and len(entered) == 1
        finally:
            release.set()
        assert (await turn.get_result()).final_output == "acknowledged"
        assert sorted(entered) == [0, 1, 2]


async def test_waiting_for_admission_does_not_start_callback_deadline(responses):
    before_entered, release = asyncio.Event(), asyncio.Event()
    entered = []

    async def before(ctx):
        if not before_entered.is_set():
            before_entered.set()
            await release.wait()
        return Allow()

    @function_tool(callback_timeout=0.02, before=before)
    async def work(payload: Payload) -> str:
        entered.append(payload.value)
        return str(payload.value)

    responses.respond(*calls())
    responses.respond(assistant_output("no queued timeout"))
    async with (
        timeout(15),
        Agent(instructions="work", tools=[work], provider=provider(responses)).session(
            max_function_concurrency=1
        ) as session,
    ):
        turn = await session.submit("wait")
        try:
            await before_entered.wait()
            await queued(session, 3)
            await asyncio.sleep(0.05)
            assert entered == []
        finally:
            release.set()
        assert (await turn.get_result()).final_output == "no queued timeout"
        assert sorted(entered) == [0, 1, 2]


async def test_root_and_child_callbacks_have_independent_permits(tmp_path, monkeypatch):
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import child_peer, handles, metadata, trust
    from tests.test_native_executor_tools import declaration

    root_entered, both_children, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    child_owners = set()

    @function_tool
    async def work(ctx: ToolContext[None]) -> str:
        if ctx.call.child_id is None:
            root_entered.set()
            await both_children.wait()
            return "parent released"
        child_owners.add(ctx.call.child_id)
        if len(child_owners) == 2:
            both_children.set()
        await release.wait()
        return "child released"

    async with (
        timeout(30),
        child_peer(("default", "worker")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        for gate in peer.release.values():
            gate.set()
        original = peer.response
        child_steps = {}

        def response():
            path = metadata(peer.requests[-1], peer.request_headers[-1])["agent_name"]
            if path == "/root":
                if not peer.root_started:
                    return original()  # Finish builtin spawning before holding a function.
                step = child_steps.get(path, 0)
                child_steps[path] = step + 1
                return (
                    [function_call("work", {}, "parent-call")]
                    if step == 0
                    else [assistant_output("root complete")]
                )
            step = child_steps.get(path, 0)
            child_steps[path] = step + 1
            return (
                [function_call("work", {}, "child-call")]
                if step == 0
                else [assistant_output("child complete")]
            )

        async def deliver_after_parent_admission(event):
            if event.get("item", {}).get("call_id") == "child-call":
                await root_entered.wait()
            await peer.hold(event)

        monkeypatch.setattr(peer, "before_event", deliver_after_parent_admission)
        monkeypatch.setattr(peer, "response", response)
        agent = Agent(
            instructions="Use children.",
            model="gpt-5.5",
            tools=[work, NativeAgents(max_children=2)],
            provider=peer.provider(websocket=False),
        )
        async with agent.session(
            environment=declaration(executor), max_function_concurrency=1
        ) as session:
            root = await session.submit("parent and children")
            try:
                await root_entered.wait()
                await both_children.wait()
                assert len(child_owners) == 2
            finally:
                release.set()
            assert (await root.get_result()).final_output == "root complete"
            children = await handles(session, 2)
            assert [(await child.get_result()).final_output for child in children] == [
                "child complete",
                "child complete",
            ]


@pytest.mark.parametrize("sync", [False, True])
async def test_run_helpers_forward_the_opening_limit(responses, sync):
    active = peak = 0
    entered = []

    @function_tool
    async def work(payload: Payload) -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        entered.append(payload.value)
        try:
            await asyncio.sleep(0.01)
            return str(payload.value)
        finally:
            active -= 1

    responses.respond(*calls())
    responses.respond(assistant_output("complete"))
    agent = Agent(instructions="work", tools=[work], provider=provider(responses))
    async with timeout(15):
        if sync:
            result = await asyncio.to_thread(agent.run_sync, "work", max_function_concurrency=1)
        else:
            result = await agent.run("work", max_function_concurrency=1)
    assert result.final_output == "complete"
    assert sorted(entered) == [0, 1, 2]
    assert peak == 1 and active == 0


async def test_waiter_rechecks_exact_native_receipt_before_validation(responses, monkeypatch):
    Payload.validated = []
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []
    first_call = None
    expire_waiters = False

    @function_tool
    async def work(ctx: ToolContext[None], payload: Payload) -> str:
        nonlocal first_call
        first_call = ctx.call.call_id
        effects.append(payload.value)
        entered.set()
        await release.wait()
        return str(payload.value)

    agent = Agent(instructions="work", tools=[work], provider=provider(responses))
    responses.respond(*calls())
    async with (
        timeout(15),
        agent.session(max_function_concurrency=1) as session,
        agent.session() as other,
    ):
        native = session._native

        class NativeReceipt:
            def __getattr__(self, name):
                return getattr(native, name)

            def _check_python_tool(self, raw):
                accepted = native._check_python_tool(raw)
                if not accepted:
                    return False
                if expire_waiters and json.loads(raw)["callId"] != first_call:
                    # Exercise the real binding's foreign-owner rejection after
                    # the task has queued, rather than inventing a host error.
                    other._native._check_python_tool(raw)
                return accepted

        monkeypatch.setattr(session, "_native", NativeReceipt())
        turn = await session.submit("wait for a permit")
        try:
            await entered.wait()
            await queued(session, 3)
            # Let the tracked tasks reach their acquisition await.
            await asyncio.sleep(0)
            expire_waiters = True
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="Python tool|managed Python"):
            await turn.get_result()
        assert len(effects) == len(Payload.validated) == 1
        assert len([event async for event in turn.events() if isinstance(event, ToolStarted)]) == 1
        assert len(responses.requests) == 1
