"""Tests for workflow connection types."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from agents import Agent, RunContextWrapper
from agents.workflow import (
    ConditionalConnection,
    HandoffConnection,
    ParallelConnection,
    SequentialConnection,
    ToolConnection,
    Workflow,
)

from .conftest import TestContext


class TestOutput(BaseModel):
    """Test output structure."""

    content: str
    source_agent: str


@pytest.mark.asyncio
async def test_handoff_connection_creation(agent_1, agent_2):
    """Test HandoffConnection creation and configuration."""
    connection = HandoffConnection(
        from_agent=agent_1,
        to_agent=agent_2,
        tool_name_override="custom_transfer",
        tool_description_override="Custom transfer description",
    )

    assert connection.from_agent == agent_1
    assert connection.to_agent == agent_2
    assert connection.tool_name_override == "custom_transfer"
    assert connection.tool_description_override == "Custom transfer description"


@pytest.mark.asyncio
async def test_tool_connection_creation(agent_1, agent_2):
    """Test ToolConnection creation and configuration."""

    def custom_extractor(result):
        return f"Extracted: {result.final_output}"

    connection = ToolConnection(
        from_agent=agent_1,
        to_agent=agent_2,
        tool_name="custom_tool",
        tool_description="Custom tool description",
        custom_output_extractor=custom_extractor,
    )

    assert connection.from_agent == agent_1
    assert connection.to_agent == agent_2
    assert connection.tool_name == "custom_tool"
    assert connection.tool_description == "Custom tool description"
    assert connection.custom_output_extractor == custom_extractor


@pytest.mark.asyncio
async def test_sequential_connection_creation(agent_1, agent_2):
    """Test SequentialConnection creation and configuration."""

    def transformer(result):
        return f"Transformed: {result.final_output}"

    connection = SequentialConnection(
        from_agent=agent_1,
        to_agent=agent_2,
        output_transformer=transformer,
    )

    assert connection.from_agent == agent_1
    assert connection.to_agent == agent_2
    assert connection.output_transformer == transformer


@pytest.mark.asyncio
async def test_conditional_connection_creation(agent_1, agent_2, agent_3):
    """Test ConditionalConnection creation and configuration."""

    def condition_func(context, previous_result):
        return context.context.counter > 5

    connection = ConditionalConnection(
        from_agent=agent_1,
        to_agent=agent_2,
        alternative_agent=agent_3,
        condition=condition_func,
    )

    assert connection.from_agent == agent_1
    assert connection.to_agent == agent_2
    assert connection.alternative_agent == agent_3
    assert connection.condition == condition_func


@pytest.mark.asyncio
async def test_parallel_connection_creation(agent_1, agent_2, agent_3):
    """Test ParallelConnection creation and configuration."""
    connection = ParallelConnection(
        from_agent=agent_1,
        to_agent=agent_2,  # Not used in parallel execution
        parallel_agents=[agent_2, agent_3],
        synthesizer_agent=agent_1,
        synthesis_template="Custom template: {results}",
    )

    assert connection.from_agent == agent_1
    assert connection.parallel_agents == [agent_2, agent_3]
    assert connection.synthesizer_agent == agent_1
    assert connection.synthesis_template == "Custom template: {results}"


@pytest.mark.asyncio
async def test_handoff_connection_prepare_agent(agent_1, agent_2):
    """Test HandoffConnection agent preparation."""
    connection = HandoffConnection(agent_1, agent_2)
    context = RunContextWrapper(TestContext())

    prepared_agent = connection.prepare_agent(context)

    # Should have cloned agent_1 with handoff to agent_2
    assert prepared_agent.name == agent_1.name
    assert len(prepared_agent.handoffs) == len(agent_1.handoffs) + 1


@pytest.mark.asyncio
async def test_tool_connection_prepare_agent(agent_1, agent_2):
    """Test ToolConnection agent preparation."""
    connection = ToolConnection(
        from_agent=agent_1,
        to_agent=agent_2,
        tool_name="test_tool",
    )
    context = RunContextWrapper(TestContext())

    prepared_agent = connection.prepare_agent(context)

    # Should have cloned agent_1 with agent_2 as a tool
    assert prepared_agent.name == agent_1.name
    assert len(prepared_agent.tools) == len(agent_1.tools) + 1


@pytest.mark.asyncio
async def test_sequential_connection_prepare_agent(agent_1, agent_2):
    """Test SequentialConnection agent preparation."""
    connection = SequentialConnection(agent_1, agent_2)
    context = RunContextWrapper(TestContext())

    prepared_agent = connection.prepare_agent(context)

    # Should return agent_2 as-is
    assert prepared_agent == agent_2


@pytest.mark.asyncio
async def test_conditional_connection_condition_evaluation():
    """Test ConditionalConnection condition evaluation."""
    agent_1 = Agent[TestContext](name="Agent 1")
    agent_2 = Agent[TestContext](name="Agent 2")
    agent_3 = Agent[TestContext](name="Agent 3")

    # Sync condition
    def sync_condition(context, previous_result):
        return context.context.counter > 5

    connection = ConditionalConnection(
        from_agent=agent_1,
        to_agent=agent_2,
        alternative_agent=agent_3,
        condition=sync_condition,
    )

    # The actual execution testing would require mocking Runner.run
    # Here we just test the connection structure
    assert connection.condition == sync_condition
    assert connection.alternative_agent == agent_3


@pytest.mark.asyncio
async def test_parallel_connection_structure(agent_1, agent_2, agent_3):
    """Test ParallelConnection structure."""
    connection = ParallelConnection(
        from_agent=agent_1,
        to_agent=agent_1,  # Not used
        parallel_agents=[agent_2, agent_3],
        synthesizer_agent=agent_1,
    )

    context = RunContextWrapper(TestContext())
    prepared_agent = connection.prepare_agent(context)

    # Should return from_agent as-is
    assert prepared_agent == agent_1
    assert len(connection.parallel_agents) == 2


@pytest.mark.asyncio
async def test_connection_abc_methods():
    """Test that Connection is properly abstract."""
    from agents.workflow.connections import Connection

    # Should not be able to instantiate Connection directly
    with pytest.raises(TypeError):
        Connection()  # type: ignore


@pytest.mark.asyncio
async def test_tool_connection_output_extractor():
    """Test ToolConnection with custom output extractor."""
    agent_1 = Agent[TestContext](name="Agent 1")
    agent_2 = Agent[TestContext](name="Agent 2", output_type=TestOutput)

    def custom_extractor(result):
        if hasattr(result.final_output, "content"):
            return result.final_output.content
        return str(result.final_output)

    connection = ToolConnection(
        from_agent=agent_1,
        to_agent=agent_2,
        custom_output_extractor=custom_extractor,
    )

    context = RunContextWrapper(TestContext())
    prepared_agent = connection.prepare_agent(context)

    # Verify the agent was prepared with the tool
    assert len(prepared_agent.tools) == len(agent_1.tools) + 1


@pytest.mark.asyncio
async def test_sequential_connection_transformer():
    """Test SequentialConnection with output transformer."""
    agent_1 = Agent[TestContext](name="Agent 1")
    agent_2 = Agent[TestContext](name="Agent 2")

    def transform_output(result):
        return f"Transformed: {result.final_output}"

    connection = SequentialConnection(
        from_agent=agent_1,
        to_agent=agent_2,
        output_transformer=transform_output,
    )

    assert connection.output_transformer == transform_output


@pytest.mark.asyncio
async def test_handoff_connection_with_filter(agent_1, agent_2):
    """Test HandoffConnection with input filter."""

    def test_filter(handoff_data):
        return handoff_data.clone(input_history="filtered")

    connection = HandoffConnection(
        from_agent=agent_1,
        to_agent=agent_2,
        input_filter=test_filter,
    )

    assert connection.input_filter == test_filter


@pytest.mark.asyncio
async def test_connection_enabled_callable(agent_1, agent_2):
    """Test connection with callable is_enabled."""

    def is_enabled_func(context, agent):
        return context.context.counter > 0

    connection = ToolConnection(
        from_agent=agent_1,
        to_agent=agent_2,
        is_enabled=is_enabled_func,
    )

    assert connection.is_enabled == is_enabled_func


@pytest.mark.asyncio
async def test_workflow_max_steps_validation():
    """Test workflow max_steps configuration."""
    agent_1 = Agent[TestContext](name="Agent 1")
    agent_2 = Agent[TestContext](name="Agent 2")

    workflow = Workflow[TestContext](
        connections=[SequentialConnection(agent_1, agent_2)],
        max_steps=5,
    )

    assert workflow.max_steps == 5
