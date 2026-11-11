from typing import Any

import pytest

from _agents_codex import session as native_session
from agents import Agent, ToolContext, function_tool
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider


@pytest.mark.parametrize("text", ["Body failed", ""])
async def test_body_failure_recovery_is_nonterminal_and_keeps_context(responses, text):
    application = {"label": "owning-run"}
    original = ValueError("synthetic-private-body-error")
    effects = []
    formatted = []
    after = []

    def recover(context: ToolContext[dict[str, str]], error: Exception) -> str:
        assert error is original
        assert context.context is application
        assert context.call_id == "body-error"
        assert context.original_arguments == {"key": "value"}
        assert context.effective_arguments == {"key": "value"}
        formatted.append(True)
        return text

    def transform(context, output):
        after.append(True)
        return output

    @function_tool(error_function=recover, after=transform, finish_turn=True)
    async def lookup(key: str) -> dict[str, bool]:
        effects.append(key)
        raise original

    responses.respond(function_call("lookup", {"key": "value"}, "body-error"))
    responses.respond(assistant_output("recovered"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    async with agent.session(context=application) as session:
        result = await (await session.submit("begin")).get_result()
    assert result.final_output == "recovered"
    assert effects == ["value"] and formatted == [True] and after == []
    assert len(responses.requests) == 2
    assert str(original) not in str(responses.requests[1]["input"])
    if text:
        assert text in str(responses.requests[1]["input"])
    assert not responses.replies


@pytest.mark.parametrize("fails", [False, True])
async def test_formatter_only_context_preserves_arguments_and_is_lazy(
    responses, monkeypatch, fails
):
    contexts = []
    formatted = []
    initialize_context = native_session.ToolContext.__init__
    original_error = ValueError("body failed")

    def observe_context(context, *args, **kwargs):
        initialize_context(context, *args, **kwargs)
        if context.call_id == "formatter-only":
            contexts.append(context)

    monkeypatch.setattr(native_session.ToolContext, "__init__", observe_context)

    def recover(context: ToolContext[None], error: Exception) -> str:
        assert error is original_error
        assert context.original_arguments == {"keys": ["original"]}
        assert context.effective_arguments == {"keys": ["original"]}
        formatted.append(context)
        return "recovered"

    @function_tool(error_function=recover)
    async def lookup(keys: list[str]) -> str:
        keys.append("body mutation")
        if fails:
            raise original_error
        return "success"

    responses.respond(function_call("lookup", {"keys": ["original"]}, "formatter-only"))
    responses.respond(assistant_output("done"))
    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    async with agent.session() as session:
        result = await (await session.submit("begin")).get_result()

    assert result.final_output == "done"
    assert contexts == formatted
    assert len(contexts) == int(fails)


@pytest.mark.parametrize(
    "phase",
    [
        "default",
        "formatter throws",
        "formatter invalid",
        "before",
        "effective input",
        "result",
        "after",
        "approval",
    ],
)
async def test_non_body_failures_keep_their_owner(responses, phase):
    from agents import Allow, ReplaceArguments

    effects = []
    formatted = []

    def recover(context, error):
        formatted.append(True)
        if phase == "formatter throws":
            raise RuntimeError("formatter failed")
        if phase == "formatter invalid":
            return 7
        return "unexpected recovery"

    def before(context):
        if phase == "before":
            raise RuntimeError("policy failed")
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
        error_function=None if phase == "default" else recover,
        before=before,
        after=after,
        needs_approval=approval if phase == "approval" else False,
    )
    async def lookup(key: str) -> dict[str, bool]:
        effects.append(key)
        if phase == "result":
            return {"accepted": "invalid"}  # type: ignore[dict-item]
        if phase == "after":
            return {"accepted": True}
        raise ValueError("body failed")

    responses.respond(function_call("lookup", {"key": "value"}, "body-error"))
    async with Agent(
        instructions="Use lookup.", tools=[lookup], provider=provider(responses)
    ).session() as session:
        with pytest.raises((RuntimeError, ValueError, TypeError)):
            await (await session.submit("begin")).get_result()
    assert effects == ([] if phase in {"before", "effective input", "approval"} else ["value"])
    assert formatted == ([True] if phase.startswith("formatter") else [])
    assert len(responses.requests) == 1
    assert not responses.replies


@pytest.mark.parametrize("phase", ["body", "formatter"])
async def test_async_cancellation_is_joined_without_recovery_publication(responses, phase):
    import asyncio

    entered = asyncio.Event()
    exited = asyncio.Event()
    formatted = []

    async def pause():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    async def recover(context, error):
        formatted.append(True)
        await pause()
        return "late recovery"

    @function_tool(error_function=recover)
    async def lookup(key: str) -> str:
        if phase == "body":
            await pause()
        raise ValueError("body failed")

    responses.respond(function_call("lookup", {"key": "value"}, "body-error"))
    async with Agent(
        instructions="Use lookup.", tools=[lookup], provider=provider(responses)
    ).session() as session:
        turn = await session.submit("begin")
        outcome = asyncio.create_task(turn.get_result())
        await entered.wait()
        await turn.cancel()
        await asyncio.gather(outcome, return_exceptions=True)
    assert exited.is_set()
    assert formatted == ([] if phase == "body" else [True])
    assert len(responses.requests) == 1
    assert not responses.replies


async def test_managed_failure_waits_for_approval_before_body_and_formatter(responses):
    from agents import ApprovalRequested

    effects = []
    formatted = []

    def recover(context, error):
        formatted.append(True)
        return "Body failed"

    @function_tool(error_function=recover, needs_approval=True)
    async def lookup(key: str) -> str:
        effects.append(key)
        raise ValueError("body failed")

    responses.respond(function_call("lookup", {"key": "value"}, "body-error"))
    responses.respond(assistant_output("recovered"))
    async with Agent(
        instructions="Use lookup.", tools=[lookup], provider=provider(responses)
    ).session() as session:
        turn = await session.submit("begin")
        approved = False
        async for request in session.interactions():
            if isinstance(request, ApprovalRequested):
                assert effects == [] and formatted == []
                await request.approve()
                approved = True
                break
        assert approved
        result = await turn.get_result()
    assert result.final_output == "recovered"
    assert effects == ["value"] and formatted == [True]
    assert len(responses.requests) == 2
    assert not responses.replies


@pytest.mark.parametrize("returns_awaitable", [False, True])
async def test_sync_body_keeps_worker_execution_and_awaitable_errors(responses, returns_awaitable):
    import threading

    loop_thread = threading.get_ident()
    body_threads = []
    formatted = []

    def recover(context, error):
        formatted.append(str(error))
        return "Body failed"

    @function_tool(error_function=recover)
    def lookup(key: str) -> Any:
        body_threads.append(threading.get_ident())
        if returns_awaitable:

            async def fail():
                assert threading.get_ident() == loop_thread
                raise ValueError("awaitable failed")

            return fail()
        raise ValueError("sync failed")

    responses.respond(function_call("lookup", {"key": "value"}, "body-error"))
    responses.respond(assistant_output("recovered"))
    async with Agent(
        instructions="Use lookup.", tools=[lookup], provider=provider(responses)
    ).session() as session:
        result = await (await session.submit("begin")).get_result()
    assert result.final_output == "recovered"
    assert len(body_threads) == 1 and body_threads[0] != loop_thread
    assert formatted == ["awaitable failed" if returns_awaitable else "sync failed"]
    assert len(responses.requests) == 2
    assert not responses.replies


async def test_formatter_only_context_is_required_before_opening(responses):
    def recover(context: ToolContext[dict[str, str]], error: Exception) -> str:
        return context.context["label"]

    @function_tool(error_function=recover)
    def lookup() -> str:
        raise ValueError("body failed")

    agent = Agent(instructions="Use lookup.", tools=[lookup], provider=provider(responses))
    with pytest.raises(TypeError, match="error_function requires application context"):
        async with agent.session() as session:  # type: ignore[call-arg]
            await session.submit("begin")
    assert responses.requests == []


def test_manual_tool_rejects_body_formatter():
    from pydantic import BaseModel

    from agents import FunctionTool

    class Arguments(BaseModel):
        key: str

    with pytest.raises(TypeError, match="Callback settings require a callback"):
        FunctionTool(
            name="manual",
            description="Manual function.",
            input_type=Arguments,
            output_type=str,
            error_function=lambda context, error: "must not run",
        )


@pytest.mark.parametrize("policy", [None, "raise_exception", "error_as_result"])
async def test_parent_cancellation_retains_original_body_cleanup_error(responses, policy):
    import asyncio

    from agents import NativeError

    entered = asyncio.Event()
    cleanup = RuntimeError("body cleanup failed")
    formatted = []

    def recover(context, error):
        formatted.append(True)
        return "must not publish"

    @function_tool(
        error_function=recover,
        callback_timeout=None if policy is None else 60,
        timeout_behavior=policy or "raise_exception",
    )
    async def lookup() -> str:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise cleanup

    responses.respond(function_call("lookup", {}, "cleanup-error"))
    manager = Agent(
        instructions="Use lookup.", tools=[lookup], provider=provider(responses)
    ).session()
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
        assert stopped.value.__cause__ is cleanup

        with pytest.raises(NativeError, match="Turn cleanup failed") as completed:
            await waiter
        assert contains(completed.value)
        assert formatted == []
        assert len(responses.requests) == 1
        assert not responses.replies
    finally:
        await asyncio.gather(waiter, return_exceptions=True)
        try:
            await manager.__aexit__(None, None, None)
        except NativeError as error:
            assert contains(error)


@pytest.mark.parametrize("with_body_formatter", [False, True])
async def test_timeout_formatter_retains_original_body_cleanup_cause(
    responses, with_body_formatter
):
    import asyncio

    cleanup = RuntimeError("body timeout cleanup failed")
    body_formatted = []
    timeout_causes = []

    def recover(context, error):
        body_formatted.append(error)
        return "must not format a timeout"

    def format_timeout(context, error):
        timeout_causes.append(error.__cause__)
        return "Timed out after cleanup"

    @function_tool(
        error_function=recover if with_body_formatter else None,
        callback_timeout=0.02,
        timeout_behavior="error_as_result",
        timeout_error_function=format_timeout,
    )
    async def lookup() -> str:
        try:
            await asyncio.Event().wait()
        finally:
            raise cleanup

    responses.respond(function_call("lookup", {}, "timeout-cleanup"))
    responses.respond(assistant_output("continued after timeout"))
    async with Agent(
        instructions="Use lookup.", tools=[lookup], provider=provider(responses)
    ).session() as session:
        result = await (await session.submit("begin")).get_result()
    assert result.final_output == "continued after timeout"
    assert timeout_causes == [cleanup]
    assert body_formatted == []
    assert len(responses.requests) == 2
    assert "Timed out after cleanup" in str(responses.requests[1]["input"])
    assert not responses.replies
