from __future__ import annotations

import asyncio
import json
import threading

import pytest

from agents import Agent, NativeError, function_tool
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import provider


@pytest.mark.parametrize("mode", ["custom", "empty", "none", "invalid", "throws", "strict"])
async def test_native_unknown_tool_formatter(responses, mode):
    seen = []
    effects = []

    @function_tool
    def valid() -> str:
        effects.append("ran")
        return "result"

    async def format_error(error):
        seen.append(error)
        if mode == "throws":
            raise RuntimeError("private formatter failure")
        return {"custom": "public missing", "empty": "", "invalid": {"text": "wrong"}}.get(mode)

    responses.respond(function_call("missing", {}, "missing-call"))
    if mode != "strict":
        responses.respond(function_call("valid", {}, "valid-call"))
        responses.respond(assistant_output("continued"))
    provider_handle = provider(responses)
    agent = Agent(instructions="Use the listed tool.", tools=[valid], provider=provider_handle)
    if mode == "strict":
        with pytest.raises(NativeError, match="missing"):
            await agent.run(
                "begin",
                tool_error_formatter=format_error,
                tool_not_found_behavior="raise_error",
            )
        assert seen == [] and effects == []
        assert len(responses.requests) == 1
        return
    result = await agent.run(
        "begin",
        tool_error_formatter=format_error,
        tool_not_found_behavior="return_error_to_model",
    )
    assert result.final_output == "continued"
    assert effects == ["ran"]
    assert len(seen) == 1
    assert seen[0].reason == "tool_not_found"
    assert seen[0].call.tool.name == "missing"
    assert seen[0].call.call_id == "missing-call"
    output = tool_output(responses.requests[1], "missing-call")
    assert "private formatter failure" not in json.dumps(output)
    if mode == "custom":
        assert output == "public missing"
    elif mode == "empty":
        assert output == ""
    else:
        assert output == seen[0].default_message


async def test_native_unknown_tool_sync_formatter_can_wait_for_the_session_loop(responses):
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    responses.respond(function_call("missing", {}, "sync-formatter"))
    responses.respond(assistant_output("continued"))

    async def format_on_loop():
        assert threading.get_ident() == loop_thread
        return "missing tool explained"

    def format_error(_error):
        assert threading.get_ident() != loop_thread
        return asyncio.run_coroutine_threadsafe(format_on_loop(), loop).result(timeout=2)

    agent = Agent(instructions="Choose an allowed tool.", provider=provider(responses))
    result = await agent.run(
        "begin",
        tool_error_formatter=format_error,
        tool_not_found_behavior="return_error_to_model",
    )
    assert result.final_output == "continued"
    assert tool_output(responses.requests[1], "sync-formatter") == "missing tool explained"


async def test_native_unknown_tool_formatter_cancellation_joins(responses):
    entered = asyncio.Event()
    stopped = asyncio.Event()
    release = asyncio.Event()
    responses.respond(function_call("missing", {}, "missing-cancelled"))

    async def format_error(error):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()
            await release.wait()

    provider_handle = provider(responses)
    agent = Agent(instructions="Choose only an allowed tool.", provider=provider_handle)
    async with agent.session(tool_error_formatter=format_error) as session:
        turn = await session.submit("begin", tool_not_found_behavior="return_error_to_model")
        result = asyncio.create_task(turn.get_result())
        await asyncio.wait_for(entered.wait(), timeout=4)
        stopping = asyncio.create_task(turn.cancel())
        try:
            await asyncio.wait_for(stopped.wait(), timeout=4)
            await asyncio.sleep(0)
            assert not stopping.done()
        finally:
            release.set()
        await stopping
        await asyncio.gather(result, return_exceptions=True)
        assert len(responses.requests) == 1


async def test_native_unknown_tool_formatter_rejects_self_cancellation(responses):
    responses.respond(function_call("missing", {}, "same-turn"))
    responses.respond(assistant_output("continued"))

    async def format_error(error):
        try:
            await asyncio.wait_for(turn.cancel(), timeout=0.3)
        except RuntimeError as rejected:
            guard_errors.append(str(rejected))
        return "recovered"

    guard_errors = []
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    async with agent.session(tool_error_formatter=format_error) as session:
        turn = await session.submit("begin", tool_not_found_behavior="return_error_to_model")
        assert (await turn.get_result()).final_output == "continued"
    assert guard_errors == ["A tool callback cannot interrupt its own Session"]
    assert tool_output(responses.requests[1], "same-turn") == "recovered"


async def test_native_unknown_tool_formatter_keeps_cancel_cleanup_failure(responses):
    entered = asyncio.Event()
    cleanup = RuntimeError("private cleanup failure")
    responses.respond(function_call("missing", {}, "cleanup"))

    async def format_error(error):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            raise cleanup

    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    with pytest.raises(NativeError):
        async with agent.session(tool_error_formatter=format_error) as session:
            turn = await session.submit("begin", tool_not_found_behavior="return_error_to_model")
            await asyncio.wait_for(entered.wait(), timeout=4)
            with pytest.raises(NativeError) as cancelled:
                await asyncio.wait_for(turn.cancel(), timeout=4)
    assert cancelled.value.__cause__ is cleanup
    with pytest.raises(NativeError) as stopped:
        await turn.get_result()
    assert stopped.value.__cause__.__context__ is cleanup
    assert len(responses.requests) == 1
