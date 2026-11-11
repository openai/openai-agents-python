from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import BaseModel

from agents import Agent, ToolContext, function_tool
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider

if TYPE_CHECKING:
    from agents import ToolErrorContext


class _NonCopyingPayload(BaseModel):
    text: str

    def __deepcopy__(self, memo=None):
        raise AssertionError("Successful function payload was copied")


def test_default_handler_stays_publicly_none_across_declaration_replacement():
    from dataclasses import asdict, fields, replace

    @function_tool
    async def lookup(key: str) -> str:
        return key

    assert lookup.error_function is None
    assert "error_function" in {declaration.name for declaration in fields(lookup)}
    assert asdict(lookup)["error_function"] is None
    assert replace(lookup, description="Updated").error_function is None
    assert replace(lookup, error_function=None).error_function is None


@pytest.mark.parametrize("enabled", [False, True])
async def test_successful_context_free_function_does_not_copy_validated_payload(responses, enabled):
    @function_tool
    async def lookup(payload: _NonCopyingPayload) -> str:
        return payload.text

    def format_error(context):
        pytest.fail("Successful function reached the formatter")

    responses.respond(function_call("lookup", {"payload": {"text": "synthetic"}}, "no-copy"))
    responses.respond(assistant_output("done"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    result = await agent.run("begin", tool_error_formatter=format_error if enabled else None)
    assert result.final_output == "done"


@pytest.mark.parametrize("phase", ["arguments", "execution"])
@pytest.mark.parametrize("mode", ["text", "empty", "none", "invalid", "throws"])
@pytest.mark.parametrize("api", ["session", "run"])
async def test_run_formatter_recovers_admitted_function_failures(responses, phase, mode, api):
    application = {"label": "owning-run"}
    original = ValueError("private-body-detail")
    seen = []
    effects = []
    after = []

    async def format_error(context: ToolErrorContext[dict[str, str]]) -> Any:
        assert context.context is application
        assert context.phase == phase
        assert context.call.tool.name == "lookup"
        assert context.call.call_id == "run-error"
        assert "private-" not in context.default_message
        assert "private-" not in repr(context)
        if phase == "execution":
            assert context.error is original
        seen.append(context)
        if mode == "throws":
            raise RuntimeError("private-formatter-detail")
        return {"text": "Application-approved failure", "empty": "", "invalid": 3}.get(mode)

    def transform(context, output):
        after.append(True)
        return output

    @function_tool(after=transform, finish_turn=True)
    async def lookup(key: str) -> dict[str, bool]:
        effects.append(key)
        raise original

    arguments: dict[str, Any] = {"key": 7 if phase == "arguments" else "value"}
    responses.respond(function_call("lookup", arguments, "run-error"))
    responses.respond(assistant_output("recovered"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    if api == "run":
        result = await agent.run("begin", context=application, tool_error_formatter=format_error)
    else:
        async with agent.session(context=application, tool_error_formatter=format_error) as session:
            result = await (await session.submit("begin")).get_result()
    assert result.final_output == "recovered"
    assert len(seen) == 1
    assert effects == ([] if phase == "arguments" else ["value"])
    assert not after
    assert len(responses.requests) == 2
    wire = str(responses.requests[1]["input"])
    assert "private-body-detail" not in wire and "private-formatter-detail" not in wire
    if mode == "text":
        assert "Application-approved failure" in wire
    elif mode not in {"empty"}:
        assert seen[0].default_message in wire
    assert not responses.replies


@pytest.mark.parametrize("phase", ["arguments", "execution"])
async def test_explicit_tool_formatter_precedes_run_formatter(responses, phase):
    called = []

    def global_format(context):
        called.append("global")
        return "unexpected global recovery"

    def input_format(context, error, original):
        called.append("input")
        return "per-tool input failure"

    def body_format(context: ToolContext, error):
        called.append("body")
        return "per-tool body failure"

    @function_tool(input_error_function=input_format, error_function=body_format)
    async def lookup(key: str) -> str:
        raise ValueError("private-detail")

    responses.respond(
        function_call("lookup", {"key": 7 if phase == "arguments" else "value"}, "precedence")
    )
    responses.respond(assistant_output("done"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    await agent.run("begin", tool_error_formatter=global_format)
    assert called == ["input" if phase == "arguments" else "body"]
    assert "per-tool" in str(responses.requests[1]["input"])


@pytest.mark.parametrize("phase", ["arguments", "execution"])
async def test_explicit_none_suppresses_run_formatter(responses, phase):
    called = []

    def global_format(context):
        called.append(True)
        return "unexpected recovery"

    @function_tool(error_function=None)
    async def lookup(key: str) -> str:
        raise ValueError("body failed")

    responses.respond(
        function_call("lookup", {"key": 7 if phase == "arguments" else "value"}, "precedence")
    )
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    with pytest.raises((RuntimeError, ValueError, TypeError)):
        await agent.run("begin", tool_error_formatter=global_format)
    assert not called
    assert len(responses.requests) == 1


@pytest.mark.parametrize("phase", ["arguments", "execution"])
async def test_cancel_joins_run_formatter_and_discards_its_reply(responses, phase):
    import asyncio

    entered = asyncio.Event()
    exited = asyncio.Event()

    async def format_error(context):
        assert context.phase == phase
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    @function_tool
    async def lookup(key: str) -> str:
        raise ValueError("private-body-detail")

    responses.respond(
        function_call("lookup", {"key": 7 if phase == "arguments" else "value"}, "cancel")
    )
    async with Agent(
        instructions="Use lookup.", tools=[lookup], provider=provider(responses)
    ).session(tool_error_formatter=format_error) as session:
        turn = await session.submit("begin")
        outcome = asyncio.create_task(turn.get_result())
        await entered.wait()
        await turn.cancel()
        await asyncio.gather(outcome, return_exceptions=True)
    assert exited.is_set()
    assert len(responses.requests) == 1
    assert not responses.replies


@pytest.mark.parametrize("phase", ["before", "effective input", "result", "after", "approval"])
async def test_run_formatter_does_not_take_other_failure_owners(responses, phase):
    from agents import Allow, ReplaceArguments

    formatted = []

    def format_error(context):
        formatted.append(True)
        return "unexpected recovery"

    def before(context):
        if phase == "before":
            raise RuntimeError("before failed")
        if phase == "effective input":
            return ReplaceArguments({"key": 7})
        return Allow()

    def after(context, output):
        if phase == "after":
            raise RuntimeError("after failed")
        return output

    def approval(context):
        raise RuntimeError("approval failed")

    @function_tool(
        before=before, after=after, needs_approval=approval if phase == "approval" else False
    )
    async def lookup(key: str) -> str:
        return 7 if phase == "result" else "done"

    responses.respond(function_call("lookup", {"key": "value"}, "other-owner"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    with pytest.raises((RuntimeError, ValueError, TypeError)):
        await agent.run("begin", tool_error_formatter=format_error)
    assert not formatted
    assert len(responses.requests) == 1
    assert not responses.replies


async def test_formatter_cleanup_failure_during_cancel_keeps_original_cause(responses):
    import asyncio

    from agents import NativeError

    entered = asyncio.Event()
    cleanup = RuntimeError("formatter cleanup failed")

    async def format_error(context):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise cleanup

    @function_tool
    async def lookup() -> str:
        raise ValueError("body failed")

    responses.respond(function_call("lookup", {}, "cleanup-error"))
    manager = Agent(
        instructions="Use lookup.", tools=[lookup], provider=provider(responses)
    ).session(tool_error_formatter=format_error)
    session = await manager.__aenter__()
    turn = await session.submit("begin")
    waiter = asyncio.create_task(turn.get_result())

    def contains(error):
        pending = [error]
        seen = set()
        while pending:
            current = pending.pop()
            if current is cleanup:
                return True
            if current is not None and id(current) not in seen:
                seen.add(id(current))
                pending.extend((current.__cause__, current.__context__))
        return False

    try:
        await entered.wait()
        with pytest.raises(
            NativeError, match="cancellation failed during owned cleanup"
        ) as stopped:
            await turn.cancel()
        assert contains(stopped.value)
        with pytest.raises(NativeError, match="Turn cleanup failed") as completed:
            await waiter
        assert contains(completed.value)
        assert len(responses.requests) == 1
    finally:
        await asyncio.gather(waiter, return_exceptions=True)
        try:
            await manager.__aexit__(None, None, None)
        except NativeError as error:
            assert contains(error)


async def test_managed_original_input_failure_is_formatted_before_approval(responses):
    called = []

    def format_error(context):
        called.append(context.phase)
        return "Invalid input before approval"

    @function_tool(needs_approval=True)
    async def lookup(key: str) -> str:
        raise AssertionError("Invalid input reached body")

    responses.respond(function_call("lookup", {"key": 7}, "managed-invalid"))
    responses.respond(assistant_output("recovered"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    result = await agent.run("begin", tool_error_formatter=format_error)
    assert result.final_output == "recovered"
    assert called == ["arguments"]
    assert "Invalid input before approval" in str(responses.requests[1]["input"])


async def test_run_sync_forwards_formatter_to_native_session(responses):
    import asyncio

    @function_tool
    async def lookup() -> str:
        raise ValueError("private-body-detail")

    responses.respond(function_call("lookup", {}, "sync-run"))
    responses.respond(assistant_output("recovered"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    result = await asyncio.to_thread(
        agent.run_sync, "begin", tool_error_formatter=lambda context: "Synchronous run failure"
    )
    assert result.final_output == "recovered"
    assert "Synchronous run failure" in str(responses.requests[1]["input"])


async def test_body_only_formatter_leaves_original_input_to_run_formatter(responses):
    called = []

    def format_error(context):
        called.append(context.phase)
        return "Global original input failure"

    def body_error(context, error):
        called.append("body")
        return "Unexpected body recovery"

    @function_tool(error_function=body_error)
    async def lookup(key: str) -> str:
        raise AssertionError("Invalid input reached body")

    responses.respond(function_call("lookup", {"key": 7}, "mixed-precedence"))
    responses.respond(assistant_output("recovered"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    result = await agent.run("begin", tool_error_formatter=format_error)
    assert result.final_output == "recovered"
    assert called == ["arguments"]
    assert "Global original input failure" in str(responses.requests[1]["input"])


@pytest.mark.parametrize("behavior", ["raise_exception", "error_as_result"])
async def test_run_formatter_preserves_callback_timeout_owner(responses, behavior):
    import asyncio

    from agents import ToolTimeoutError

    formatted = []
    cleaned = []

    def format_error(context):
        formatted.append(True)
        return "Unexpected timeout recovery"

    @function_tool(callback_timeout=0.02, timeout_behavior=behavior)
    async def lookup() -> str:
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)

    responses.respond(function_call("lookup", {}, "deadline-owner"))
    if behavior == "error_as_result":
        responses.respond(assistant_output("recovered"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    async with agent.session(tool_error_formatter=format_error) as session:
        turn = await session.submit("begin")
        if behavior == "raise_exception":
            with pytest.raises(ToolTimeoutError):
                await turn.get_result()
        else:
            assert (await turn.get_result()).final_output == "recovered"
    assert cleaned == [True]
    assert not formatted
    assert len(responses.requests) == (1 if behavior == "raise_exception" else 2)


@pytest.mark.parametrize("phase", ["arguments", "execution"])
@pytest.mark.parametrize("opt_out", [False, True])
async def test_replaced_declaration_preserves_direct_opt_out_and_same_value_none(
    responses, phase, opt_out
):
    from dataclasses import replace

    formatted = []

    def format_error(context):
        formatted.append(context.phase)
        return "Copied declaration recovery"

    async def lookup(key: str) -> str:
        raise ValueError("body failed")

    declaration = function_tool(lookup, error_function=None) if opt_out else function_tool(lookup)
    copied = replace(declaration, description="Updated")
    copied = replace(copied, error_function=None)
    responses.respond(
        function_call("lookup", {"key": 7 if phase == "arguments" else "value"}, "copy")
    )
    if not opt_out:
        responses.respond(assistant_output("recovered"))
    agent = Agent(instructions="Use lookup.", tools=[copied], provider=provider(responses))
    if opt_out:
        with pytest.raises(ValueError):
            await agent.run("begin", tool_error_formatter=format_error)
    else:
        result = await agent.run("begin", tool_error_formatter=format_error)
        assert result.final_output == "recovered"
        assert "Copied declaration recovery" in str(responses.requests[1]["input"])
    assert formatted == ([] if opt_out else [phase])
    assert len(responses.requests) == (1 if opt_out else 2)
