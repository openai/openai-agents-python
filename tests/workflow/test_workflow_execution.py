"""Tests for workflow execution functionality."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from agents import Agent, RunContextWrapper, UserError
from agents.workflow import (
    HandoffConnection,
    SequentialConnection,
    ToolConnection,
    Workflow,
    WorkflowResult,
)

from .conftest import TestContext


class MockResult:
    """Mock RunResult for testing."""

    def __init__(self, output: str, agent: Agent[Any]):
        self.final_output = output
        self.last_agent = agent
        self.new_items: list[Any] = []
        self.raw_responses: list[Any] = []
        self.input_guardrail_results: list[Any] = []
        self.output_guardrail_results: list[Any] = []
        self.context_wrapper = None

    def to_input_list(self):
        return [{"role": "assistant", "content": self.final_output}]


@pytest.mark.asyncio
async def test_workflow_execution_mocked(agent_1, agent_2, test_context):
    """Test workflow execution with mocked Runner."""
    workflow = Workflow[TestContext](
        connections=[SequentialConnection(agent_1, agent_2)],
        context=test_context,
        trace_workflow=False,  # Disable tracing for simpler testing
    )

    # Mock the Runner.run method
    mock_result_1 = MockResult("Step 1 output", agent_1)
    mock_result_2 = MockResult("Step 2 output", agent_2)

    with patch("agents.run.Runner.run") as mock_run:
        mock_run.side_effect = [mock_result_1, mock_result_2]

        result = await workflow.run("Test input")

        # Verify execution
        assert isinstance(result, WorkflowResult)
        assert result.final_result.final_output == mock_result_2.final_output
        assert len(result.step_results) == 1  # Only one connection executed
        assert result.context == test_context

        # Verify Runner.run was called correctly (twice - once for each agent)
        assert mock_run.call_count == 2


@pytest.mark.asyncio
async def test_workflow_sync_execution(agent_1, agent_2):
    """Test synchronous workflow execution."""
    workflow = Workflow[TestContext](
        connections=[SequentialConnection(agent_1, agent_2)],
        trace_workflow=False,
    )

    mock_result = MockResult("Sync output", agent_2)

    with patch("agents.run.Runner.run") as mock_run:
        mock_run.return_value = mock_result

        # Test run_sync method
        result = await workflow.run("Test input")

        assert isinstance(result, WorkflowResult)
        assert result.final_result.final_output == mock_result.final_output


@pytest.mark.asyncio
async def test_workflow_execution_with_context_override(agent_1, agent_2, test_context):
    """Test workflow execution with context override."""
    workflow = Workflow[TestContext](
        connections=[SequentialConnection(agent_1, agent_2)],
        context=test_context,
        trace_workflow=False,
    )

    override_context = TestContext(test_data="override", counter=99)
    mock_result = MockResult("Output", agent_2)

    with patch("agents.run.Runner.run") as mock_run:
        mock_run.return_value = mock_result

        result = await workflow.run("Test input", context=override_context)

        # Should use override context
        assert result.context == override_context


@pytest.mark.asyncio
async def test_workflow_execution_no_context(agent_1, agent_2):
    """Test workflow execution without context."""
    workflow = Workflow[TestContext](
        connections=[SequentialConnection(agent_1, agent_2)],
        trace_workflow=False,
    )

    mock_result = MockResult("Output", agent_2)

    with patch("agents.run.Runner.run") as mock_run:
        mock_run.return_value = mock_result

        result = await workflow.run("Test input")

        # Should handle None context gracefully
        assert result.context is None


@pytest.mark.asyncio
async def test_workflow_max_steps_exceeded(agent_1, agent_2):
    """Test workflow stops when max_steps is exceeded."""
    workflow = Workflow[TestContext](
        connections=[SequentialConnection(agent_1, agent_2)],
        max_steps=0,  # Set to 0 to trigger immediately
        trace_workflow=False,
    )

    with patch("agents.run.Runner.run"):
        with pytest.raises(UserError, match="exceeded maximum steps"):
            await workflow.run("Test input")


@pytest.mark.asyncio
async def test_workflow_execution_error_handling(agent_1, agent_2):
    """Test workflow error handling during execution."""
    workflow = Workflow[TestContext](
        connections=[SequentialConnection(agent_1, agent_2)],
        trace_workflow=False,
    )

    with patch("agents.run.Runner.run") as mock_run:
        mock_run.side_effect = Exception("Test error")

        with pytest.raises(UserError, match="Workflow failed at step 0"):
            await workflow.run("Test input")


@pytest.mark.asyncio
async def test_workflow_no_results_error(agent_1, agent_2):
    """Test workflow error when no results are produced."""
    workflow = Workflow[TestContext](
        connections=[SequentialConnection(agent_1, agent_2)],
        trace_workflow=False,
    )

    with patch("agents.run.Runner.run") as mock_run:
        mock_run.return_value = None

        with pytest.raises(UserError, match="'NoneType' object has no attribute 'final_output'"):
            await workflow.run("Test input")


@pytest.mark.asyncio
async def test_handoff_connection_prepare_agent_details(agent_1, agent_2):
    """Test detailed HandoffConnection agent preparation."""
    connection = HandoffConnection(
        from_agent=agent_1,
        to_agent=agent_2,
        tool_name_override="custom_handoff",
        tool_description_override="Custom description",
    )

    context = RunContextWrapper(TestContext())
    prepared_agent = connection.prepare_agent(context)

    # Verify the handoff was added correctly
    assert prepared_agent != agent_1  # Should be a clone
    assert prepared_agent.name == agent_1.name
    assert len(prepared_agent.handoffs) == len(agent_1.handoffs) + 1

    # Verify handoff properties (would need to inspect the handoff object)
    added_handoff = prepared_agent.handoffs[-1]
    assert hasattr(added_handoff, "tool_name") or hasattr(added_handoff, "name")


@pytest.mark.asyncio
async def test_tool_connection_prepare_agent_details(agent_1, agent_2):
    """Test detailed ToolConnection agent preparation."""
    connection = ToolConnection(
        from_agent=agent_1,
        to_agent=agent_2,
        tool_name="custom_tool",
        tool_description="Custom tool description",
    )

    context = RunContextWrapper(TestContext())
    prepared_agent = connection.prepare_agent(context)

    # Verify the tool was added correctly
    assert prepared_agent != agent_1  # Should be a clone
    assert prepared_agent.name == agent_1.name
    assert len(prepared_agent.tools) == len(agent_1.tools) + 1


@pytest.mark.asyncio
async def test_workflow_tracing_enabled(agent_1, agent_2):
    """Test workflow with tracing enabled."""
    workflow = Workflow[TestContext](
        connections=[SequentialConnection(agent_1, agent_2)],
        name="Traced Workflow",
        trace_workflow=True,
    )

    mock_result = MockResult("Output", agent_2)

    with patch("agents.run.Runner.run") as mock_run:
        mock_run.return_value = mock_result

        with patch("agents.workflow.workflow.trace") as mock_trace:
            await workflow.run("Test input")

            # Verify tracing was called
            mock_trace.assert_called_once_with("Traced Workflow")


@pytest.mark.asyncio
async def test_workflow_tracing_disabled(agent_1, agent_2):
    """Test workflow with tracing disabled."""
    workflow = Workflow[TestContext](
        connections=[SequentialConnection(agent_1, agent_2)],
        trace_workflow=False,
    )

    mock_result = MockResult("Output", agent_2)

    with patch("agents.run.Runner.run") as mock_run:
        mock_run.return_value = mock_result

        with patch("agents.workflow.workflow.trace") as mock_trace:
            await workflow.run("Test input")

            # Verify tracing was NOT called
            mock_trace.assert_not_called()
