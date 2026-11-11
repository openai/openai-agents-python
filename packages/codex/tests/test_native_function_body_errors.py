from typing import Any

import pytest

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
