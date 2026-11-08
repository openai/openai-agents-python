"""Timeout policies retain invocation ownership and publish only admitted results."""

import asyncio
import os
import subprocess
import sys
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import BaseModel, model_serializer

import agents
from agents import (
    Agent,
    Allow,
    ToolContext,
    ToolOutput,
    ToolTimeoutBehavior,
    ToolTimeoutError,
    TurnStoppedError,
    function_tool,
)
from agents.observation import ToolFinished
from agents.tools import _CapturedFunction
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider


def test_timeout_policy_public_types_and_captured_configuration():
    from agents._function_tools import ToolTimeoutError as FacadeError

    assert FacadeError is ToolTimeoutError
    assert {"ToolTimeoutError", "ToolTimeoutBehavior"} <= set(agents.__all__)
    assert set(get_args(ToolTimeoutBehavior)) == {"raise_exception", "error_as_result"}
    error = ToolTimeoutError("lookup", 0.25)
    assert isinstance(error, TimeoutError)
    assert error.tool_name == "lookup" and error.timeout_seconds == 0.25

    def format_timeout(ctx, error):
        return "retry"

    @function_tool
    async def lookup() -> int:
        return 1

    configured = replace(
        lookup,
        callback_timeout=0.25,
        timeout_behavior="error_as_result",
        timeout_error_function=format_timeout,
    )
    captured = configured._capture(None)
    assert isinstance(captured, _CapturedFunction)
    assert captured.callback_timeout == 0.25
    assert captured.timeout_behavior == "error_as_result"
    assert captured.timeout_error_function is format_timeout
    assert lookup.timeout_behavior == "raise_exception"


@pytest.mark.parametrize(
    "options, error_type",
    [
        ({"timeout_behavior": "invalid"}, ValueError),
        ({"timeout_behavior": None}, ValueError),
        ({"timeout_error_function": "not-callable"}, TypeError),
    ],
)
def test_timeout_policy_rejects_invalid_configuration(options, error_type):
    async def lookup() -> int:
        return 1

    with pytest.raises(error_type, match="timeout_"):
        function_tool(**options)(lookup)


@pytest.mark.parametrize("formatter", ["default", "sync", "async"])
@pytest.mark.parametrize("exit_kind", ["cancel", "late", "cleanup_error"])
async def test_timeout_result_waits_for_cleanup_and_skips_success_hooks(
    responses, formatter, exit_kind
):
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleaned = asyncio.Event()
    hooks = []
    errors = []
    serialized = []

    class Value(BaseModel):
        @model_serializer
        def serialize(self) -> str:
            serialized.append("serialized")
            return "late-private-value"

    async def before(ctx: ToolContext[None]) -> Allow:
        hooks.append("before")
        return Allow()

    async def after(ctx: ToolContext[None], output: ToolOutput) -> ToolOutput:
        hooks.append("after")
        return output

    def format_sync(ctx: ToolContext[None], error: ToolTimeoutError) -> str:
        assert cleaned.is_set()
        assert ctx.call_id == "policy-timeout"
        assert error.tool_name == "lookup" and error.timeout_seconds == 0.01
        errors.append(error)
        return "timeout-retry"

    async def format_async(ctx: ToolContext[None], error: ToolTimeoutError) -> str:
        await asyncio.sleep(0)
        return format_sync(ctx, error)

    @function_tool(
        callback_timeout=0.01,
        timeout_behavior="error_as_result",
        timeout_error_function={"default": None, "sync": format_sync, "async": format_async}[
            formatter
        ],
        before=before,
        after=after,
    )
    async def lookup() -> Value:
        try:
            await asyncio.Future()
            raise AssertionError("Callback unexpectedly completed")
        except asyncio.CancelledError:
            if exit_kind == "late":
                return Value()
            raise
        finally:
            cleanup_started.set()
            await cleanup_release.wait()
            cleaned.set()
            if exit_kind == "cleanup_error":
                raise ValueError("cleanup-failed")

    responses.respond(function_call("lookup", {}, "policy-timeout"))
    responses.respond(assistant_output("recovered"))
    async with (
        timeout(15),
        Agent(
            instructions="Use lookup.", tools=[lookup], provider=provider(responses)
        ).session() as session,
    ):
        observations = session.events()
        turn = await session.submit("read")
        waiter = asyncio.create_task(turn.get_result())
        try:
            await cleanup_started.wait()
            assert not waiter.done() and len(responses.requests) == 1
            assert errors == []
        finally:
            cleanup_release.set()
        assert (await waiter).final_output == "recovered"
        assert hooks == ["before"] and serialized == []
        text = tool_output(responses.requests[1], "policy-timeout")
        assert ("callback timeout" if formatter == "default" else "timeout-retry") in text
        assert "late-private-value" not in text
        if formatter != "default" and exit_kind == "cleanup_error":
            assert isinstance(errors[0].__cause__, ValueError)
    finished = [event async for event in observations if isinstance(event, ToolFinished)]
    assert len(finished) == 1 and finished[0].status == "failed"


@pytest.mark.parametrize("policy", ["raise_exception", "error_as_result"])
@pytest.mark.parametrize("outcome", ["success", "error"])
async def test_on_time_body_keeps_outcome_when_unrelated_work_delays_parent(
    responses, policy, outcome
):
    loop = asyncio.get_running_loop()
    formatted = []
    body_durations = []
    stalls = []
    error = ValueError("ordinary-callback-error")

    def formatter(ctx, timeout_error):
        formatted.append(timeout_error)
        return "false-timeout"

    def stall_loop():
        time.sleep(0.06)
        stalls.append("finished")

    @function_tool(callback_timeout=0.02, timeout_behavior=policy, timeout_error_function=formatter)
    async def lookup() -> str:
        started = loop.time()
        loop.call_soon(stall_loop)
        body_durations.append(loop.time() - started)
        if outcome == "error":
            raise error
        return "on-time"

    responses.respond(function_call("lookup", {}, "on-time-body"))
    responses.respond(assistant_output("completed"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    async with timeout(10):
        if outcome == "error":
            with pytest.raises(ValueError) as caught:
                await agent.run("read")
            assert caught.value is error
            assert len(responses.requests) == 1
        else:
            assert (await agent.run("read")).final_output == "completed"
            assert tool_output(responses.requests[1], "on-time-body") == "on-time"
    assert len(body_durations) == 1 and body_durations[0] < 0.02
    assert stalls == ["finished"]
    assert formatted == []


@pytest.mark.parametrize("policy", ["raise_exception", "error_as_result"])
@pytest.mark.parametrize("error_type", [TimeoutError, ToolTimeoutError])
async def test_user_timeout_like_error_is_not_invocation_expiry(responses, policy, error_type):
    calls = []
    error = error_type("user", 3) if error_type is ToolTimeoutError else TimeoutError("user")

    def formatter(ctx, timeout_error):
        calls.append("formatted")
        return "unexpected"

    @function_tool(callback_timeout=30, timeout_behavior=policy, timeout_error_function=formatter)
    async def lookup() -> str:
        raise error

    responses.respond(function_call("lookup", {}, "user-error"))
    async with timeout(10):
        with pytest.raises(error_type) as caught:
            await Agent(
                instructions="Use lookup.", tools=[lookup], provider=provider(responses)
            ).run("read")
    assert caught.value is error
    assert calls == [] and len(responses.requests) == 1


@pytest.mark.parametrize("policy, duration", [("raise_exception", 0.01), ("error_as_result", None)])
async def test_formatter_is_inactive_outside_result_expiry(responses, policy, duration):
    def formatter(ctx, error):
        raise AssertionError("Inactive timeout formatter ran")

    @function_tool(
        callback_timeout=duration, timeout_behavior=policy, timeout_error_function=formatter
    )
    async def lookup() -> str:
        if duration is not None:
            await asyncio.Future()
        return "normal-result"

    responses.respond(function_call("lookup", {}, "inactive-formatter"))
    responses.respond(assistant_output("completed"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    async with timeout(10):
        if duration is not None:
            with pytest.raises(ToolTimeoutError) as caught:
                await agent.run("read")
            assert caught.value.tool_name == "lookup"
            assert isinstance(caught.value, TimeoutError)
        else:
            assert (await agent.run("read")).final_output == "completed"
            assert "normal-result" in tool_output(responses.requests[1], "inactive-formatter")


@pytest.mark.parametrize("invalid", [False, True])
async def test_formatter_failure_propagates(responses, invalid):
    error = ValueError("formatter-failed")

    async def formatter(ctx, timeout_error) -> Any:
        if invalid:
            return ToolOutput.text("not-a-string")
        raise error

    @function_tool(
        callback_timeout=0.01, timeout_behavior="error_as_result", timeout_error_function=formatter
    )
    async def lookup() -> int:
        await asyncio.Future()
        return 1

    responses.respond(function_call("lookup", {}, "formatter-failure"))
    async with timeout(10):
        with pytest.raises(TypeError if invalid else ValueError) as caught:
            await Agent(
                instructions="Use lookup.", tools=[lookup], provider=provider(responses)
            ).run("read")
    if invalid:
        assert "must return str" in str(caught.value)
    else:
        assert caught.value is error
    assert len(responses.requests) == 1


@pytest.mark.parametrize("stage", ["cleanup", "formatter"])
async def test_parent_cancellation_joins_cleanup_and_prevents_timeout_publication(responses, stage):
    entered = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()
    formatted = []

    async def formatter(ctx, error):
        formatted.append("entered")
        entered.set()
        try:
            await asyncio.Future()
        finally:
            await release.wait()
            cleaned.set()
        return "forbidden-result"

    @function_tool(
        callback_timeout=0.01, timeout_behavior="error_as_result", timeout_error_function=formatter
    )
    async def lookup() -> str:
        try:
            await asyncio.Future()
        finally:
            if stage == "cleanup":
                entered.set()
                await release.wait()
                cleaned.set()
        return "late"

    responses.respond(function_call("lookup", {}, "cancel-policy"))
    responses.respond(assistant_output("next-turn"))
    async with (
        timeout(15),
        Agent(
            instructions="Use lookup.", tools=[lookup], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("read")
        waiter = asyncio.create_task(turn.get_result())
        await entered.wait()
        stopping = asyncio.create_task(session._cancel(turn))
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not waiter.done()
            assert not cleaned.is_set()
        finally:
            release.set()
        await stopping
        with pytest.raises(TurnStoppedError):
            await waiter
        assert cleaned.is_set()
        assert formatted == ([] if stage == "cleanup" else ["entered"])
        assert len(responses.requests) == 1
        assert "forbidden-result" not in str([item async for item in session.items()])
        assert (await (await session.submit("next")).get_result()).final_output == "next-turn"


async def test_result_timeout_revokes_queued_sync_body_before_worker_entry(responses, monkeypatch):
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    occupied = asyncio.Event()
    queued = asyncio.Event()
    formatted = asyncio.Event()
    release = threading.Event()
    effects = []

    def occupy():
        loop.call_soon_threadsafe(occupied.set)
        assert release.wait(10)

    blocker = loop.run_in_executor(None, occupy)
    await occupied.wait()
    original = loop.run_in_executor

    def observe(executor, callback, *args):
        future = original(executor, callback, *args)
        queued.set()
        return future

    async def formatter(ctx, error):
        formatted.set()
        return "queued-timeout"

    @function_tool(
        callback_timeout=0.02, timeout_behavior="error_as_result", timeout_error_function=formatter
    )
    def lookup() -> str:
        effects.append("entered")
        return "forbidden"

    responses.respond(function_call("lookup", {}, "queued-result"))
    responses.respond(assistant_output("recovered"))
    async with (
        timeout(15),
        Agent(
            instructions="Use lookup.", tools=[lookup], provider=provider(responses)
        ).session() as session,
    ):
        monkeypatch.setattr(loop, "run_in_executor", observe)
        turn = await session.submit("read")
        waiter = asyncio.create_task(turn.get_result())
        try:
            await queued.wait()
            await asyncio.sleep(0.04)
            assert not waiter.done() and not formatted.is_set()
        finally:
            release.set()
            await blocker
        assert (await waiter).final_output == "recovered"
        assert effects == [] and formatted.is_set()
        assert "queued-timeout" in tool_output(responses.requests[1], "queued-result")


async def test_busy_result_callback_crossing_cutoff_never_serializes_late_value(responses):
    serialized = []

    class Value(BaseModel):
        @model_serializer
        def serialize(self) -> str:
            serialized.append("serialized")
            return "late"

    @function_tool(callback_timeout=0.001, timeout_behavior="error_as_result")
    async def lookup() -> Any:
        time.sleep(0.02)
        return Value()

    responses.respond(function_call("lookup", {}, "busy-result"))
    responses.respond(assistant_output("recovered"))
    async with timeout(10):
        assert (
            await Agent(
                instructions="Use lookup.", tools=[lookup], provider=provider(responses)
            ).run("read")
        ).final_output == ("recovered")
    assert serialized == []
    assert "callback timeout" in tool_output(responses.requests[1], "busy-result")


async def test_result_timeout_joins_entered_sync_worker_and_discards_its_value(responses):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    exited = threading.Event()
    serialized = []
    effects = []

    class Value(BaseModel):
        @model_serializer
        def serialize(self) -> str:
            serialized.append("serialized")
            return "late-sync-value"

    def formatter(ctx, error):
        assert exited.is_set()
        return "sync-timeout"

    @function_tool(
        callback_timeout=0.1, timeout_behavior="error_as_result", timeout_error_function=formatter
    )
    def lookup() -> Value:
        effects.append("effect-already-performed")
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(10)
            return Value()
        finally:
            exited.set()

    responses.respond(function_call("lookup", {}, "sync-result"))
    responses.respond(assistant_output("recovered"))
    async with (
        timeout(15),
        Agent(
            instructions="Use lookup.", tools=[lookup], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("read")
        waiter = asyncio.create_task(turn.get_result())
        try:
            await entered.wait()
            await asyncio.sleep(0.12)
            assert not waiter.done() and not exited.is_set()
            assert effects == ["effect-already-performed"] and serialized == []
        finally:
            release.set()
        assert (await waiter).final_output == "recovered"
        assert exited.is_set() and serialized == []
        assert effects == ["effect-already-performed"]
        assert "sync-timeout" in tool_output(responses.requests[1], "sync-result")


class ApplicationContext:
    pass


@pytest.mark.parametrize(
    "policy, duration, active",
    [
        ("raise_exception", None, False),
        ("raise_exception", 1, False),
        ("error_as_result", None, False),
        ("error_as_result", 1, True),
    ],
)
def test_only_active_timeout_formatter_requires_application_context(policy, duration, active):
    def formatter(ctx: ToolContext[ApplicationContext], error: ToolTimeoutError) -> str:
        return "retry"

    @function_tool(
        callback_timeout=duration, timeout_behavior=policy, timeout_error_function=formatter
    )
    async def lookup() -> str:
        return "normal"

    if active:
        with pytest.raises(TypeError, match="timeout_error_function requires application context"):
            lookup._check_context(None)
    else:
        lookup._check_context(None)
        lookup._capture(None)
    lookup._check_context(ApplicationContext())


async def test_timeout_result_leaves_parallel_invocation_live(responses):
    @function_tool(callback_timeout=0.03, timeout_behavior="error_as_result")
    async def lookup(key: str) -> str:
        if key == "slow":
            await asyncio.Future()
        return "accepted:" + key

    responses.respond(
        function_call("lookup", {"key": "slow"}, "slow-call"),
        function_call("lookup", {"key": "fast"}, "fast-call"),
    )
    responses.respond(assistant_output("completed"))
    async with timeout(10):
        result = await Agent(
            instructions="Use lookup.", tools=[lookup], provider=provider(responses)
        ).run("read")
    assert result.final_output == "completed"
    assert "callback timeout" in tool_output(responses.requests[1], "slow-call")
    assert "accepted:fast" in tool_output(responses.requests[1], "fast-call")


async def test_child_timeout_result_preserves_root_and_child_identity(tmp_path, monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from agents import ChildInvocation, NativeAgents
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import ChildPeer, handles, metadata, trust
    from tests.test_native_executor_tools import declaration

    contexts = []

    class TimeoutChildPeer(ChildPeer):
        def response(self):
            body = self.requests[-1]
            if metadata(body, self.request_headers[-1])["agent_name"] == "/root":
                return super().response()
            self.child_requests.setdefault("alpha", []).append(body)
            if len(self.child_requests["alpha"]) == 1:
                return [function_call("lookup", {}, "child-timeout")]
            return [assistant_output("alpha complete")]

    def formatter(ctx: ToolContext[None], error: ToolTimeoutError) -> str:
        assert isinstance(ctx.submission, ChildInvocation)
        assert ctx.call_id == "child-timeout"
        assert ctx.call.child_id == ctx.submission.child_id
        contexts.append(ctx)
        return "child-timeout-result"

    @function_tool(
        callback_timeout=0.01, timeout_behavior="error_as_result", timeout_error_function=formatter
    )
    async def lookup() -> int:
        await asyncio.Future()
        return 1

    peer = TimeoutChildPeer(("default",))
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    async with (
        timeout(25),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        agent: Agent[None, str] = Agent(
            instructions="Start alpha.",
            model="gpt-6.1-sol",
            provider=peer.provider(websocket=False),
            tools=[lookup, NativeAgents(max_children=1)],
        )
        async with agent.session(environment=declaration(executor)) as session:
            root = await session.submit("Start alpha")
            assert (await root.get_result()).final_output == "root complete"
            [child] = await handles(session, 1)
            assert (await child.get_result()).final_output == "alpha complete"
            assert len(contexts) == 1
            assert contexts[0].submission.submission_id == child.submission_id
            assert "child-timeout-result" in tool_output(
                peer.child_requests["alpha"][1], "child-timeout"
            )


async def test_before_denial_never_starts_timeout_body_formatter_or_after(responses):
    from agents import Deny

    calls = []

    def before(ctx: ToolContext[None]) -> Deny:
        calls.append("before")
        return Deny("denied-before-body")

    def after(ctx: ToolContext[None], output: ToolOutput) -> ToolOutput:
        raise AssertionError("Denied invocation reached after hook")

    def formatter(ctx, error):
        raise AssertionError("Denied invocation reached timeout formatter")

    @function_tool(
        callback_timeout=0.001,
        timeout_behavior="error_as_result",
        timeout_error_function=formatter,
        before=before,
        after=after,
    )
    async def lookup() -> int:
        calls.append("body")
        return 1

    responses.respond(function_call("lookup", {}, "denied-timeout"))
    responses.respond(assistant_output("completed"))
    async with timeout(10):
        assert (
            await Agent(
                instructions="Use lookup.", tools=[lookup], provider=provider(responses)
            ).run("read")
        ).final_output == "completed"
    assert calls == ["before"]
    assert "denied-before-body" in tool_output(responses.requests[1], "denied-timeout")


@pytest.mark.skipif(not hasattr(asyncio, "eager_task_factory"), reason="Requires Python 3.12+")
async def test_result_policy_initializes_deadline_before_eager_body(responses):
    loop = asyncio.get_running_loop()
    original_factory = loop.get_task_factory()

    async def before(ctx):
        loop.set_task_factory(asyncio.eager_task_factory)
        return Allow()

    @function_tool(callback_timeout=1, timeout_behavior="error_as_result", before=before)
    async def lookup() -> str:
        loop.set_task_factory(original_factory)
        return "on-time"

    responses.respond(function_call("lookup", {}, "eager-body"))
    responses.respond(assistant_output("done"))
    try:
        async with timeout(10):
            result = await Agent(
                instructions="Use lookup.", tools=[lookup], provider=provider(responses)
            ).run("read")
        assert result.final_output == "done"
        assert tool_output(responses.requests[1], "eager-body") == "on-time"
    finally:
        loop.set_task_factory(original_factory)


@pytest.mark.parametrize("error_name", ["SystemExit", "KeyboardInterrupt"])
@pytest.mark.parametrize("stage", ["body", "timeout-cleanup"])
def test_result_policy_transports_process_control_errors_to_turn(error_name, stage):
    # Keep a regression in asyncio's process-control handling inside a subprocess.
    script = textwrap.dedent("""        import asyncio
        import sys
        sys.path.insert(0, sys.argv[2])
        from aiohttp import web
        from aiohttp.test_utils import TestServer
        from agents import Agent, function_tool
        from tests.native_fixtures import ResponsesFixture, function_call
        from tests.test_native_agent import provider

        async def main():
            error_type = {"SystemExit": SystemExit, "KeyboardInterrupt": KeyboardInterrupt}
            error = error_type[sys.argv[1]]()
            @function_tool(callback_timeout=0.01, timeout_behavior="error_as_result")
            async def lookup() -> str:
                if sys.argv[3] == "timeout-cleanup":
                    try:
                        await asyncio.Future()
                    finally:
                        raise error
                raise error

            responses = ResponsesFixture()
            responses.respond(function_call("lookup", {}, "process-control"))
            app = web.Application()
            app.router.add_post("/v1/responses", responses.handle)
            async with TestServer(app) as server:
                responses.url = str(server.make_url("/v1"))
                survivor = asyncio.create_task(asyncio.sleep(0.05, result="alive"))
                async with Agent(
                    instructions="Use lookup.", tools=[lookup], provider=provider(responses)
                ).session() as session:
                    turn = await session.submit("read")
                    # Observe the owning turn without the separate result-waiter
                    # task, which has its own process-control exception boundary.
                    await asyncio.wait((turn._completion,))
                    assert turn._completion.exception() is error
                assert await survivor == "alive"
                assert len(responses.requests) == 1
            print("parent retained process-control error")

        asyncio.run(main())
    """)
    completed = subprocess.run(
        [sys.executable, "-c", script, error_name, str(Path(__file__).resolve().parents[1]), stage],
        env={key: value for key, value in os.environ.items() if key != "OPENAI_API_KEY"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "parent retained process-control error" in completed.stdout
