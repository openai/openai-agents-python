"""Regression: callable needs_approval must fail closed on bad tool JSON (#3863)."""

from __future__ import annotations

import pytest
from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall

from agents import function_tool
from agents.run_context import RunContextWrapper
from agents.run_internal.tool_execution import function_needs_approval


async def _needs_if_refund(_ctx, params, _call_id) -> bool:
    return "refund" in str(params.get("subject", "")).lower()


@function_tool(needs_approval=_needs_if_refund)
async def send_email(subject: str, body: str) -> str:
    return f"Sent {subject}"


@function_tool(needs_approval=True)
async def always_gated(subject: str) -> str:
    return subject


def _call(arguments: str) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        type="function_call",
        name="send_email",
        call_id="call_test",
        arguments=arguments,
    )


@pytest.mark.asyncio
async def test_invalid_json_arguments_fail_closed_for_callable_needs_approval() -> None:
    ctx = RunContextWrapper(context=None)
    # Contains "REFUND" text but is not valid JSON — previously parsed as {}.
    bad = _call('{"subject": "REFUND request", "body": "please refund"')
    assert await function_needs_approval(send_email, ctx, bad) is True


@pytest.mark.asyncio
async def test_non_object_json_fail_closed_for_callable_needs_approval() -> None:
    ctx = RunContextWrapper(context=None)
    for arguments in ("null", "[]", '"x"', "1"):
        assert await function_needs_approval(send_email, ctx, _call(arguments)) is True


@pytest.mark.asyncio
async def test_valid_refund_json_still_requires_approval() -> None:
    ctx = RunContextWrapper(context=None)
    good = _call('{"subject": "REFUND request", "body": "please refund"}')
    assert await function_needs_approval(send_email, ctx, good) is True


@pytest.mark.asyncio
async def test_valid_non_refund_json_skips_approval() -> None:
    ctx = RunContextWrapper(context=None)
    good = _call('{"subject": "hello", "body": "world"}')
    assert await function_needs_approval(send_email, ctx, good) is False


@pytest.mark.asyncio
async def test_bool_needs_approval_true_unaffected_by_invalid_json() -> None:
    ctx = RunContextWrapper(context=None)
    bad = ResponseFunctionToolCall(
        type="function_call",
        name="always_gated",
        call_id="call_bool",
        arguments='{"subject": "x"',
    )
    assert await function_needs_approval(always_gated, ctx, bad) is True
