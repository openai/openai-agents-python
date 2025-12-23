"""Tests for realtime handoff functionality."""

import asyncio
import inspect
from collections.abc import Awaitable, Coroutine
from typing import Any, cast
from unittest.mock import Mock

import pytest

from agents import Agent
from agents.exceptions import ModelBehaviorError, UserError
from agents.realtime import RealtimeAgent, realtime_handoff
from agents.run_context import RunContextWrapper


def test_realtime_handoff_creation():
    """Test basic realtime handoff creation."""
    realtime_agent = RealtimeAgent(name="test_agent")
    handoff_obj = realtime_handoff(realtime_agent)

    assert handoff_obj.agent_name == "test_agent"
    assert handoff_obj.tool_name == "transfer_to_test_agent"
    assert handoff_obj.input_filter is None  # Should not support input filters
    assert handoff_obj.is_enabled is True


def test_realtime_handoff_with_custom_params():
    """Test realtime handoff with custom parameters."""
    realtime_agent = RealtimeAgent(
        name="helper_agent",
        handoff_description="Helps with general tasks",
    )

    handoff_obj = realtime_handoff(
        realtime_agent,
        tool_name_override="custom_handoff",
        tool_description_override="Custom handoff description",
        is_enabled=False,
    )

    assert handoff_obj.agent_name == "helper_agent"
    assert handoff_obj.tool_name == "custom_handoff"
    assert handoff_obj.tool_description == "Custom handoff description"
    assert handoff_obj.is_enabled is False


@pytest.mark.asyncio
async def test_realtime_handoff_execution():
    """Test that realtime handoff returns the correct agent."""
    realtime_agent = RealtimeAgent(name="target_agent")
    handoff_obj = realtime_handoff(realtime_agent)

    # Mock context
    mock_context = Mock()

    # Execute handoff
    result = await handoff_obj.on_invoke_handoff(mock_context, "")

    assert result is realtime_agent
    assert isinstance(result, RealtimeAgent)


def test_realtime_handoff_with_on_handoff_callback():
    """Test realtime handoff with custom on_handoff callback."""
    realtime_agent = RealtimeAgent(name="callback_agent")
    callback_called = []

    def on_handoff_callback(ctx):
        callback_called.append(True)

    handoff_obj = realtime_handoff(
        realtime_agent,
        on_handoff=on_handoff_callback,
    )

    asyncio.run(
        cast(
            Coroutine[Any, Any, RealtimeAgent[Any]],
            handoff_obj.on_invoke_handoff(RunContextWrapper(None), ""),
        )
    )
    assert callback_called == [True]
    assert handoff_obj.agent_name == "callback_agent"


def test_regular_agent_handoff_still_works():
    """Test that regular Agent handoffs still work with the new generic types."""
    from agents import handoff

    regular_agent = Agent(name="regular_agent")
    handoff_obj = handoff(regular_agent)

    assert handoff_obj.agent_name == "regular_agent"
    assert handoff_obj.tool_name == "transfer_to_regular_agent"
    # Regular agent handoffs should support input filters
    assert hasattr(handoff_obj, "input_filter")


def test_type_annotations_work():
    """Test that type annotations work correctly."""
    from agents.handoffs import Handoff
    from agents.realtime.handoffs import realtime_handoff

    realtime_agent = RealtimeAgent(name="typed_agent")
    handoff_obj = realtime_handoff(realtime_agent)

    # This should be typed as Handoff[Any, RealtimeAgent[Any]]
    assert isinstance(handoff_obj, Handoff)


def test_realtime_handoff_invalid_param_counts_raise():
    rt = RealtimeAgent(name="x")

    # on_handoff with input_type but wrong param count
    def bad2(a):  # only one parameter
        return None

    assert bad2(None) is None
    with pytest.raises(UserError):
        realtime_handoff(rt, on_handoff=bad2, input_type=int)  # type: ignore[arg-type]

    # on_handoff without input but wrong param count
    def bad1(a, b):  # two parameters
        return None

    assert bad1(None, None) is None
    with pytest.raises(UserError):
        realtime_handoff(rt, on_handoff=bad1)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_realtime_handoff_missing_input_json_raises_model_error():
    rt = RealtimeAgent(name="x")

    async def with_input(ctx: RunContextWrapper[Any], data: int):  # simple non-object type
        return None

    h = realtime_handoff(rt, on_handoff=with_input, input_type=int)

    with pytest.raises(ModelBehaviorError):
        await h.on_invoke_handoff(RunContextWrapper(None), "null")

    await with_input(RunContextWrapper(None), 1)


@pytest.mark.asyncio
async def test_realtime_handoff_is_enabled_async(monkeypatch):
    rt = RealtimeAgent(name="x")

    async def is_enabled(ctx, agent):
        return True

    h = realtime_handoff(rt, is_enabled=is_enabled)
    assert callable(h.is_enabled)
    result = h.is_enabled(RunContextWrapper(None), rt)
    assert isinstance(result, Awaitable)
    assert await result


@pytest.mark.asyncio
async def test_realtime_handoff_rejects_none_input() -> None:
    rt = RealtimeAgent(name="x")

    async def with_input(ctx: RunContextWrapper[Any], data: int) -> None:
        return None

    handoff_obj = realtime_handoff(rt, on_handoff=with_input, input_type=int)

    with pytest.raises(ModelBehaviorError):
        await handoff_obj.on_invoke_handoff(RunContextWrapper(None), cast(str, None))

    await with_input(RunContextWrapper(None), 2)


@pytest.mark.asyncio
async def test_realtime_handoff_sync_is_enabled_callable() -> None:
    rt = RealtimeAgent(name="x")
    calls: list[bool] = []

    def is_enabled(ctx: RunContextWrapper[Any], agent: RealtimeAgent[Any]) -> bool:
        calls.append(True)
        assert agent is rt
        return False

    handoff_obj = realtime_handoff(rt, is_enabled=is_enabled)
    assert callable(handoff_obj.is_enabled)
    enabled_result = handoff_obj.is_enabled(RunContextWrapper(None), rt)
    if inspect.isawaitable(enabled_result):
        assert await enabled_result is False
    else:
        assert enabled_result is False
    assert calls, "is_enabled callback should be invoked"


def test_realtime_handoff_sync_on_handoff_executes() -> None:
    rt = RealtimeAgent(name="sync")
    called: list[int] = []

    def on_handoff(ctx: RunContextWrapper[Any], value: int) -> None:
        called.append(value)

    handoff_obj = realtime_handoff(rt, on_handoff=on_handoff, input_type=int)
    result: RealtimeAgent[Any] = asyncio.run(
        cast(
            Coroutine[Any, Any, RealtimeAgent[Any]],
            handoff_obj.on_invoke_handoff(RunContextWrapper(None), "5"),
        )
    )

    assert result is rt
    assert called == [5]


def test_realtime_handoff_on_handoff_without_input_runs() -> None:
    rt = RealtimeAgent(name="no_input")
    called: list[bool] = []

    def on_handoff(ctx: RunContextWrapper[Any]) -> None:
        called.append(True)

    handoff_obj = realtime_handoff(rt, on_handoff=on_handoff)
    result: RealtimeAgent[Any] = asyncio.run(
        cast(
            Coroutine[Any, Any, RealtimeAgent[Any]],
            handoff_obj.on_invoke_handoff(RunContextWrapper(None), ""),
        )
    )

    assert result is rt
    assert called == [True]
