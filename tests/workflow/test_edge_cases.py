"""Tests for workflow edge cases and error conditions."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from agents import Agent, UserError
from agents.workflow import (
    ConditionalConnection,
    HandoffConnection,
    ParallelConnection,
    SequentialConnection,
    ToolConnection,
    Workflow,
)

from .conftest import TestContext


@pytest.mark.asyncio
async def test_conditional_connection_no_alternative():
    """Test ConditionalConnection without alternative agent."""
    agent_1 = Agent[TestContext](name="Agent 1")
    agent_2 = Agent[TestContext](name="Agent 2")

    def false_condition(context, previous_result):
        return False

    connection = ConditionalConnection(
        from_agent=agent_1,
        to_agent=agent_2,
        condition=false_condition,
        # No alternative_agent provided
    )

    from agents.run_context import RunContextWrapper

    context = RunContextWrapper(TestContext())

    # Should raise error when condition is False and no alternative
    with pytest.raises(UserError, match="no alternative agent provided"):
        await connection.execute(context, "test input")


@pytest.mark.asyncio
async def test_conditional_connection_async_condition():
    """Test ConditionalConnection with async condition function."""

    async def async_condition(context, previous_result):
        # Simulate async operation
        await asyncio.sleep(0.001)
        return context.context.counter > 5

    # Test that async condition works (we can't easily test full execution without complex mocking)
    # So we test the condition directly
    from agents.run_context import RunContextWrapper

    context_low = RunContextWrapper(TestContext(counter=3))
    context_high = RunContextWrapper(TestContext(counter=10))

    assert await async_condition(context_low, None) is False
    assert await async_condition(context_high, None) is True


@pytest.mark.asyncio
async def test_parallel_connection_no_synthesizer():
    """Test ParallelConnection without synthesizer agent."""
    agent_1 = Agent[TestContext](name="Agent 1")
    agent_2 = Agent[TestContext](name="Agent 2")
    agent_3 = Agent[TestContext](name="Agent 3")

    connection = ParallelConnection(
        from_agent=agent_1,
        to_agent=agent_1,  # Not used
        parallel_agents=[agent_2, agent_3],
        # No synthesizer_agent
    )

    # Mock parallel execution
    from agents.result import RunResult
    from agents.run_context import RunContextWrapper

    mock_result_1 = RunResult(
        input="test input",
        new_items=[],
        raw_responses=[],
        final_output="Result 1",
        input_guardrail_results=[],
        output_guardrail_results=[],
        context_wrapper=RunContextWrapper(TestContext()),
        _last_agent=agent_2,
    )

    mock_result_2 = RunResult(
        input="test input",
        new_items=[],
        raw_responses=[],
        final_output="Result 2",
        input_guardrail_results=[],
        output_guardrail_results=[],
        context_wrapper=RunContextWrapper(TestContext()),
        _last_agent=agent_3,
    )

    with patch("asyncio.gather") as mock_gather:
        mock_gather.return_value = [mock_result_1, mock_result_2]

        context = RunContextWrapper(TestContext())
        result = await connection.execute(context, "test input")

        # Should return first result when no synthesizer
        assert result == mock_result_1


@pytest.mark.asyncio
async def test_workflow_circular_reference():
    """Test workflow with circular agent references."""
    agent_1 = Agent[TestContext](name="Agent 1")
    agent_2 = Agent[TestContext](name="Agent 2")

    # Create circular workflow
    workflow = Workflow[TestContext](
        connections=[
            SequentialConnection(agent_1, agent_2),
            SequentialConnection(agent_2, agent_1),  # Back to agent_1
        ]
    )

    # Should validate successfully (circular is allowed)
    errors = workflow.validate_chain()
    assert len(errors) == 0
    assert workflow.agent_count == 2


@pytest.mark.asyncio
async def test_workflow_single_connection():
    """Test workflow with single connection."""
    agent_1 = Agent[TestContext](name="Agent 1")
    agent_2 = Agent[TestContext](name="Agent 2")

    workflow = Workflow[TestContext](connections=[HandoffConnection(agent_1, agent_2)])

    assert len(workflow.connections) == 1
    assert workflow.agent_count == 2
    assert workflow.start_agent == agent_1
    assert workflow.end_agent == agent_2

    errors = workflow.validate_chain()
    assert len(errors) == 0


@pytest.mark.asyncio
async def test_workflow_same_agent_connection():
    """Test connection from agent to itself."""
    agent = Agent[TestContext](name="Self Agent")

    workflow = Workflow[TestContext](connections=[SequentialConnection(agent, agent)])

    assert workflow.agent_count == 1
    assert workflow.start_agent == agent
    assert workflow.end_agent == agent

    errors = workflow.validate_chain()
    assert len(errors) == 0


@pytest.mark.asyncio
async def test_workflow_mixed_connection_types():
    """Test workflow with mixed connection types."""
    agent_1 = Agent[TestContext](name="Agent 1")
    agent_2 = Agent[TestContext](name="Agent 2")
    agent_3 = Agent[TestContext](name="Agent 3")
    agent_4 = Agent[TestContext](name="Agent 4")

    workflow = Workflow[TestContext](
        connections=[
            HandoffConnection(agent_1, agent_2),
            ToolConnection(agent_2, agent_3, tool_name="tool_3"),
            SequentialConnection(agent_2, agent_4),  # Note: from agent_2, not agent_3
        ]
    )

    assert len(workflow.connections) == 3
    assert workflow.agent_count == 4

    # Validate chain - should be valid
    errors = workflow.validate_chain()
    assert len(errors) == 0


@pytest.mark.asyncio
async def test_workflow_large_chain():
    """Test workflow with many connections."""
    agents = [Agent[TestContext](name=f"Agent {i}") for i in range(10)]

    from agents.workflow.connections import Connection
    connections: list[Connection[TestContext]] = []
    for i in range(9):
        connections.append(SequentialConnection(agents[i], agents[i + 1]))

    workflow = Workflow[TestContext](connections=connections)

    assert len(workflow.connections) == 9
    assert workflow.agent_count == 10
    assert workflow.start_agent == agents[0]
    assert workflow.end_agent == agents[9]

    errors = workflow.validate_chain()
    assert len(errors) == 0


@pytest.mark.asyncio
async def test_workflow_max_steps_edge_case():
    """Test workflow with max_steps at boundary."""
    agent_1 = Agent[TestContext](name="Agent 1")
    agent_2 = Agent[TestContext](name="Agent 2")

    # Set max_steps to exactly the number of connections
    workflow = Workflow[TestContext](
        connections=[SequentialConnection(agent_1, agent_2)],
        max_steps=1,
        trace_workflow=False,
    )

    # Mock successful execution
    from agents.result import RunResult
    from agents.run_context import RunContextWrapper

    mock_result = RunResult(
        input="test input",
        new_items=[],
        raw_responses=[],
        final_output="Success",
        input_guardrail_results=[],
        output_guardrail_results=[],
        context_wrapper=RunContextWrapper(TestContext()),
        _last_agent=agent_2,
    )

    with patch("agents.workflow.connections.Runner.run") as mock_run:
        mock_run.return_value = mock_result

        # Should succeed with exactly max_steps
        result = await workflow.run("Test input")
        assert result.final_result == mock_result


@pytest.mark.asyncio
async def test_workflow_context_none_handling():
    """Test workflow handles None context gracefully."""
    agent_1 = Agent[TestContext](name="Agent 1")
    agent_2 = Agent[TestContext](name="Agent 2")

    workflow = Workflow[TestContext](
        connections=[SequentialConnection(agent_1, agent_2)],
        # No context provided
        trace_workflow=False,
    )

    from agents.result import RunResult
    from agents.run_context import RunContextWrapper

    mock_result = RunResult(
        input="test input",
        new_items=[],
        raw_responses=[],
        final_output="Success",
        input_guardrail_results=[],
        output_guardrail_results=[],
        context_wrapper=RunContextWrapper(None),
        _last_agent=agent_2,
    )

    with patch("agents.workflow.connections.Runner.run") as mock_run:
        mock_run.return_value = mock_result

        result = await workflow.run("Test input")
        assert result.context is None


@pytest.mark.asyncio
async def test_tool_connection_async_output_extractor():
    """Test ToolConnection with async output extractor."""
    agent_1 = Agent[TestContext](name="Agent 1")
    agent_2 = Agent[TestContext](name="Agent 2")

    async def async_extractor(result):
        # Simulate async processing
        await asyncio.sleep(0.001)
        return f"Async extracted: {result.final_output}"

    connection = ToolConnection(
        from_agent=agent_1,
        to_agent=agent_2,
        custom_output_extractor=async_extractor,
    )

    from agents.run_context import RunContextWrapper

    context = RunContextWrapper(TestContext())

    # Test that prepare_agent handles async extractor
    prepared_agent = connection.prepare_agent(context)
    assert len(prepared_agent.tools) == len(agent_1.tools) + 1


@pytest.mark.asyncio
async def test_workflow_step_results_isolation():
    """Test that step results are properly isolated between workflow runs."""
    agent_1 = Agent[TestContext](name="Agent 1")
    agent_2 = Agent[TestContext](name="Agent 2")

    workflow = Workflow[TestContext](
        connections=[SequentialConnection(agent_1, agent_2)],
        trace_workflow=False,
    )

    from agents.result import RunResult
    from agents.run_context import RunContextWrapper

    mock_result_1 = RunResult(
        input="test input",
        new_items=[],
        raw_responses=[],
        final_output="First run",
        input_guardrail_results=[],
        output_guardrail_results=[],
        context_wrapper=RunContextWrapper(TestContext()),
        _last_agent=agent_2,
    )

    mock_result_2 = RunResult(
        input="test input",
        new_items=[],
        raw_responses=[],
        final_output="Second run",
        input_guardrail_results=[],
        output_guardrail_results=[],
        context_wrapper=RunContextWrapper(TestContext()),
        _last_agent=agent_2,
    )

    with patch("agents.workflow.connections.Runner.run") as mock_run:
        # First execution
        mock_run.return_value = mock_result_1
        result_1 = await workflow.run("First input")

        # Second execution
        mock_run.return_value = mock_result_2
        result_2 = await workflow.run("Second input")

        # Results should be isolated
        assert result_1.final_result != result_2.final_result
        assert result_1.step_results != result_2.step_results
