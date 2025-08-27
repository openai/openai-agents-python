import inspect
import json
from typing import Any

import pytest
from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from pydantic import BaseModel

from agents import (
    Agent,
    Handoff,
    HandoffInputData,
    MessageOutputItem,
    ModelBehaviorError,
    RunContextWrapper,
    UserError,
    handoff,
)
from agents.run import AgentRunner


def message_item(content: str, agent: Agent[Any]) -> MessageOutputItem:
    return MessageOutputItem(
        agent=agent,
        raw_item=ResponseOutputMessage(
            id="123",
            status="completed",
            role="assistant",
            type="message",
            content=[ResponseOutputText(text=content, type="output_text", annotations=[])],
        ),
    )


def get_len(data: HandoffInputData) -> int:
    input_len = len(data.input_history) if isinstance(data.input_history, tuple) else 1
    pre_handoff_len = len(data.pre_handoff_items)
    new_items_len = len(data.new_items)
    return input_len + pre_handoff_len + new_items_len


@pytest.mark.asyncio
async def test_single_handoff_setup():
    agent_1 = Agent(name="test_1")
    agent_2 = Agent(name="test_2", handoffs=[agent_1])

    assert not agent_1.handoffs
    assert agent_2.handoffs == [agent_1]

    assert not (await AgentRunner._get_handoffs(agent_1, RunContextWrapper(agent_1)))

    handoff_objects = await AgentRunner._get_handoffs(agent_2, RunContextWrapper(agent_2))
    assert len(handoff_objects) == 1
    obj = handoff_objects[0]
    assert obj.tool_name == Handoff.default_tool_name(agent_1)
    assert obj.tool_description == Handoff.default_tool_description(agent_1)
    assert obj.agent_name == agent_1.name


@pytest.mark.asyncio
async def test_multiple_handoffs_setup():
    agent_1 = Agent(name="test_1")
    agent_2 = Agent(name="test_2")
    agent_3 = Agent(name="test_3", handoffs=[agent_1, agent_2])

    assert agent_3.handoffs == [agent_1, agent_2]
    assert not agent_1.handoffs
    assert not agent_2.handoffs

    handoff_objects = await AgentRunner._get_handoffs(agent_3, RunContextWrapper(agent_3))
    assert len(handoff_objects) == 2
    assert handoff_objects[0].tool_name == Handoff.default_tool_name(agent_1)
    assert handoff_objects[1].tool_name == Handoff.default_tool_name(agent_2)

    assert handoff_objects[0].tool_description == Handoff.default_tool_description(agent_1)
    assert handoff_objects[1].tool_description == Handoff.default_tool_description(agent_2)

    assert handoff_objects[0].agent_name == agent_1.name
    assert handoff_objects[1].agent_name == agent_2.name


@pytest.mark.asyncio
async def test_custom_handoff_setup():
    agent_1 = Agent(name="test_1")
    agent_2 = Agent(name="test_2")
    agent_3 = Agent(
        name="test_3",
        handoffs=[
            agent_1,
            handoff(
                agent_2,
                tool_name_override="custom_tool_name",
                tool_description_override="custom tool description",
            ),
        ],
    )

    assert len(agent_3.handoffs) == 2
    assert not agent_1.handoffs
    assert not agent_2.handoffs

    handoff_objects = await AgentRunner._get_handoffs(agent_3, RunContextWrapper(agent_3))
    assert len(handoff_objects) == 2

    first_handoff = handoff_objects[0]
    assert isinstance(first_handoff, Handoff)
    assert first_handoff.tool_name == Handoff.default_tool_name(agent_1)
    assert first_handoff.tool_description == Handoff.default_tool_description(agent_1)
    assert first_handoff.agent_name == agent_1.name

    second_handoff = handoff_objects[1]
    assert isinstance(second_handoff, Handoff)
    assert second_handoff.tool_name == "custom_tool_name"
    assert second_handoff.tool_description == "custom tool description"
    assert second_handoff.agent_name == agent_2.name


class Foo(BaseModel):
    bar: str


@pytest.mark.asyncio
async def test_handoff_input_type():
    async def _on_handoff(ctx: RunContextWrapper[Any], input: Foo):
        pass

    agent = Agent(name="test")
    obj = handoff(agent, input_type=Foo, on_handoff=_on_handoff)
    for key, value in Foo.model_json_schema().items():
        assert obj.input_json_schema[key] == value

    # Invalid JSON should raise an error
    with pytest.raises(ModelBehaviorError):
        await obj.on_invoke_handoff(RunContextWrapper(agent), "not json")

    # Empty JSON should raise an error
    with pytest.raises(ModelBehaviorError):
        await obj.on_invoke_handoff(RunContextWrapper(agent), "")

    # Valid JSON should call the on_handoff function
    invoked = await obj.on_invoke_handoff(
        RunContextWrapper(agent), Foo(bar="baz").model_dump_json()
    )
    assert invoked == agent


@pytest.mark.asyncio
async def test_on_handoff_called():
    was_called = False

    async def _on_handoff(ctx: RunContextWrapper[Any], input: Foo):
        nonlocal was_called
        was_called = True

    agent = Agent(name="test")
    obj = handoff(agent, input_type=Foo, on_handoff=_on_handoff)
    for key, value in Foo.model_json_schema().items():
        assert obj.input_json_schema[key] == value

    invoked = await obj.on_invoke_handoff(
        RunContextWrapper(agent), Foo(bar="baz").model_dump_json()
    )
    assert invoked == agent

    assert was_called, "on_handoff should have been called"


@pytest.mark.asyncio
async def test_on_handoff_without_input_called():
    was_called = False

    def _on_handoff(ctx: RunContextWrapper[Any]):
        nonlocal was_called
        was_called = True

    agent = Agent(name="test")
    obj = handoff(agent, on_handoff=_on_handoff)

    invoked = await obj.on_invoke_handoff(RunContextWrapper(agent), "")
    assert invoked == agent

    assert was_called, "on_handoff should have been called"


@pytest.mark.asyncio
async def test_async_on_handoff_without_input_called():
    was_called = False

    async def _on_handoff(ctx: RunContextWrapper[Any]):
        nonlocal was_called
        was_called = True

    agent = Agent(name="test")
    obj = handoff(agent, on_handoff=_on_handoff)

    invoked = await obj.on_invoke_handoff(RunContextWrapper(agent), "")
    assert invoked == agent

    assert was_called, "on_handoff should have been called"


@pytest.mark.asyncio
async def test_invalid_on_handoff_raises_error():
    was_called = False

    async def _on_handoff(ctx: RunContextWrapper[Any], blah: str):
        nonlocal was_called
        was_called = True  # pragma: no cover

    agent = Agent(name="test")

    with pytest.raises(UserError):
        # Purposely ignoring the type error here to simulate invalid input
        handoff(agent, on_handoff=_on_handoff)  # type: ignore


def test_handoff_input_data():
    agent = Agent(name="test")

    data = HandoffInputData(
        input_history="",
        pre_handoff_items=(),
        new_items=(),
        run_context=RunContextWrapper(context=()),
    )
    assert get_len(data) == 1

    data = HandoffInputData(
        input_history=({"role": "user", "content": "foo"},),
        pre_handoff_items=(),
        new_items=(),
        run_context=RunContextWrapper(context=()),
    )
    assert get_len(data) == 1

    data = HandoffInputData(
        input_history=(
            {"role": "user", "content": "foo"},
            {"role": "assistant", "content": "bar"},
        ),
        pre_handoff_items=(),
        new_items=(),
        run_context=RunContextWrapper(context=()),
    )
    assert get_len(data) == 2

    data = HandoffInputData(
        input_history=({"role": "user", "content": "foo"},),
        pre_handoff_items=(
            message_item("foo", agent),
            message_item("foo2", agent),
        ),
        new_items=(
            message_item("bar", agent),
            message_item("baz", agent),
        ),
        run_context=RunContextWrapper(context=()),
    )
    assert get_len(data) == 5

    data = HandoffInputData(
        input_history=(
            {"role": "user", "content": "foo"},
            {"role": "assistant", "content": "bar"},
        ),
        pre_handoff_items=(message_item("baz", agent),),
        new_items=(
            message_item("baz", agent),
            message_item("qux", agent),
        ),
        run_context=RunContextWrapper(context=()),
    )

    assert get_len(data) == 5


def test_handoff_input_schema_is_strict():
    agent = Agent(name="test")
    obj = handoff(agent, input_type=Foo, on_handoff=lambda ctx, input: None)
    for key, value in Foo.model_json_schema().items():
        assert obj.input_json_schema[key] == value

    assert obj.strict_json_schema, "Input schema should be strict"

    assert (
        "additionalProperties" in obj.input_json_schema
        and not obj.input_json_schema["additionalProperties"]
    ), "Input schema should be strict and have additionalProperties=False"


def test_get_transfer_message_is_valid_json() -> None:
    agent = Agent(name="foo")
    obj = handoff(agent)
    transfer = obj.get_transfer_message(agent)
    assert json.loads(transfer) == {"assistant": agent.name}


def test_handoff_is_enabled_bool():
    """Test that handoff respects is_enabled boolean parameter."""
    agent = Agent(name="test")

    # Test enabled handoff (default)
    handoff_enabled = handoff(agent)
    assert handoff_enabled.is_enabled is True

    # Test explicitly enabled handoff
    handoff_explicit_enabled = handoff(agent, is_enabled=True)
    assert handoff_explicit_enabled.is_enabled is True

    # Test disabled handoff
    handoff_disabled = handoff(agent, is_enabled=False)
    assert handoff_disabled.is_enabled is False


@pytest.mark.asyncio
async def test_handoff_is_enabled_callable():
    """Test that handoff respects is_enabled callable parameter."""
    agent = Agent(name="test")

    # Test callable that returns True
    def always_enabled(ctx: RunContextWrapper[Any], agent: Agent[Any]) -> bool:
        return True

    handoff_callable_enabled = handoff(agent, is_enabled=always_enabled)
    assert callable(handoff_callable_enabled.is_enabled)
    result = handoff_callable_enabled.is_enabled(RunContextWrapper(agent), agent)
    assert inspect.isawaitable(result)
    result = await result
    assert result is True

    # Test callable that returns False
    def always_disabled(ctx: RunContextWrapper[Any], agent: Agent[Any]) -> bool:
        return False

    handoff_callable_disabled = handoff(agent, is_enabled=always_disabled)
    assert callable(handoff_callable_disabled.is_enabled)
    result = handoff_callable_disabled.is_enabled(RunContextWrapper(agent), agent)
    assert inspect.isawaitable(result)
    result = await result
    assert result is False

    # Test async callable
    async def async_enabled(ctx: RunContextWrapper[Any], agent: Agent[Any]) -> bool:
        return True

    handoff_async_enabled = handoff(agent, is_enabled=async_enabled)
    assert callable(handoff_async_enabled.is_enabled)
    result = await handoff_async_enabled.is_enabled(RunContextWrapper(agent), agent)  # type: ignore
    assert result is True


@pytest.mark.asyncio
async def test_handoff_is_enabled_filtering_integration():
    """Integration test that disabled handoffs are filtered out by the runner."""

    # Set up agents
    agent_1 = Agent(name="agent_1")
    agent_2 = Agent(name="agent_2")
    agent_3 = Agent(name="agent_3")

    # Create main agent with mixed enabled/disabled handoffs
    main_agent = Agent(
        name="main_agent",
        handoffs=[
            handoff(agent_1, is_enabled=True),  # enabled
            handoff(agent_2, is_enabled=False),  # disabled
            handoff(agent_3, is_enabled=lambda ctx, agent: True),  # enabled callable
        ],
    )

    context_wrapper = RunContextWrapper(main_agent)

    # Get filtered handoffs using the runner's method
    filtered_handoffs = await AgentRunner._get_handoffs(main_agent, context_wrapper)

    # Should only have 2 handoffs (agent_1 and agent_3), agent_2 should be filtered out
    assert len(filtered_handoffs) == 2

    # Check that the correct agents are present
    agent_names = {h.agent_name for h in filtered_handoffs}
    assert agent_names == {"agent_1", "agent_3"}
    assert "agent_2" not in agent_names
