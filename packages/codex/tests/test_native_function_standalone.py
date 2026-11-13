"""Direct application calls reuse native SDK tool validation and callback policies."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast

import pytest
from pydantic import ValidationError

from agents import (
    Agent,
    Allow,
    NativeProvider,
    ReplaceArguments,
    ToolContext,
    ToolInvocationContext,
    ToolInvocationInputContext,
    ToolTimeoutError,
    function_tool,
)
from agents.tools import ToolOutput
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_function_results import _assert_sdk_failure_privacy


@dataclass
class Account:
    multiplier: int


async def test_standalone_returns_validated_output_and_redacts_rejected_result():
    @function_tool
    def double(amount: int) -> int:
        return cast(int, str(amount * 2))

    assert await double.invoke('{"amount": 2}') == 4
    assert await double.invoke({"amount": 3}) == 6

    @function_tool
    def invalid(amount: str) -> int:
        return cast(int, amount + "-output")

    marker = "private-standalone-input"
    with pytest.raises(ValueError, match="Tool result failed validation") as caught:
        await invalid.invoke({"amount": marker})
    _assert_sdk_failure_privacy(caught.value, marker, marker + "-output")


async def test_standalone_context_and_policies_also_work_in_live_session(responses):
    account = Account(multiplier=3)
    seen = []

    def before(ctx: ToolInvocationContext[Account]) -> ReplaceArguments:
        assert ctx.context is account
        assert ctx.original_arguments == ctx.effective_arguments == {"amount": 2}
        return ReplaceArguments({"amount": 3})

    @function_tool(before=before)
    def scale(ctx: ToolInvocationContext[Account], amount: int) -> int:
        seen.append(amount)
        assert ctx.original_arguments == {"amount": 2}
        assert ctx.effective_arguments == {"amount": 3}
        return amount * ctx.context.multiplier

    assert await scale.invoke('{"amount": 2}', context=account) == 9

    responses.respond(function_call("scale", {"amount": 2}, "scale-1"))
    responses.respond(assistant_output("done"))
    agent = Agent(
        instructions="Scale the amount.",
        tools=[scale],
        provider=NativeProvider(base_url=responses.url, api_key="synthetic"),
    )
    assert (await agent.run("scale", context=account)).final_output == "done"
    assert tool_output(responses.requests[1], "scale-1") == "9"
    assert seen == [3, 3]


async def test_standalone_configured_input_and_body_failures():
    calls = []

    def input_error(ctx: ToolInvocationInputContext[str], error: Exception, raw: object) -> str:
        assert ctx.context == "application"
        return f"{type(error).__name__} {raw!r}"

    async def body_error(ctx: ToolInvocationContext[str], error: Exception) -> str:
        assert ctx.context == "application"
        assert ctx.effective_arguments == {"amount": 2}
        return str(error)

    @function_tool(input_error_function=input_error, error_function=body_error)
    def broken(amount: int) -> int:
        calls.append(amount)
        raise LookupError("application failure")

    assert await broken.invoke("{", context="application") == ToolOutput.error(
        "JSONDecodeError '{'"
    )
    assert await broken.invoke({"amount": "bad"}, context="application") == ToolOutput.error(
        "ValidationError {'amount': 'bad'}"
    )
    assert await broken.invoke({"amount": 2}, context="application") == ToolOutput.error(
        "application failure"
    )
    assert calls == [2]


async def test_standalone_deadline_joins_callback_and_skips_late_output():
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    processed = []

    def timeout(ctx: ToolInvocationContext[str], error: ToolTimeoutError) -> str:
        assert ctx.context == "application"
        assert error.tool_name == "slow"
        return "timed out"

    def after(ctx: ToolInvocationContext[str], output: ToolOutput) -> ToolOutput:
        processed.append(output)
        return output

    @function_tool(
        callback_timeout=0.05,
        timeout_behavior="error_as_result",
        timeout_error_function=timeout,
        after=after,
    )
    async def slow() -> str:
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            return "late value"

    pending = asyncio.create_task(slow.invoke("{}", context="application"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await asyncio.wait_for(cancelled.wait(), 3)
        assert not pending.done()
    finally:
        release.set()
    assert await asyncio.wait_for(pending, 3) == ToolOutput.error("timed out")
    assert processed == []


async def test_standalone_caller_cancellation_joins_entered_deadline_cleanup():
    cleaning = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    @function_tool(callback_timeout=0.05)
    async def owned() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
            completed.set()
            raise

    invocation = asyncio.create_task(owned.invoke("{}"))
    try:
        await asyncio.wait_for(cleaning.wait(), 3)
        invocation.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(invocation, 3)
    assert completed.is_set()


async def test_standalone_applies_after_policy_to_validated_output():
    def after(ctx: ToolInvocationContext[str], output: ToolOutput) -> ToolOutput:
        assert ctx.context == "application"
        assert output == ToolOutput.text("4")
        return ToolOutput.text("updated")

    @function_tool(after=after)
    def double(amount: int) -> int:
        return amount * 2

    assert await double.invoke('{"amount": 2}', context="application") == ToolOutput.text("updated")


async def test_standalone_detaches_nested_inputs_before_formatting_validation_failure():
    def input_error(ctx: ToolInvocationInputContext[None], error: Exception, raw: dict) -> str:
        assert isinstance(error, ValidationError)
        raw["options"]["values"].append(2)
        error.errors()[0]["input"]["options"]["values"].append(3)
        return "formatted"

    @function_tool(input_error_function=input_error)
    def required(amount: int, options: dict[str, list[int]]) -> int:
        return amount + sum(options["values"])

    original = {"options": {"values": [1]}}
    assert await required.invoke(original) == ToolOutput.error("formatted")
    assert original == {"options": {"values": [1]}}


async def test_standalone_rejects_live_authority_before_callback():
    effects = []

    @function_tool(needs_approval=True)
    def approved() -> None:
        effects.append("approval bypassed")

    @function_tool
    def native(ctx: ToolContext[None]) -> None:
        effects.append("missing live context")

    def live_policy(ctx: ToolContext[None] | None) -> Allow:
        effects.append("live policy entered")
        return Allow()

    @function_tool(before=live_policy)
    def policy() -> None:
        effects.append("policy bypassed")

    with pytest.raises(TypeError, match="without approval"):
        await approved.invoke("{}")
    with pytest.raises(TypeError, match="ToolInvocationContext"):
        await native.invoke("{}")
    with pytest.raises(TypeError, match="requires? a live ToolContext"):
        await policy.invoke("{}")
    assert effects == []
