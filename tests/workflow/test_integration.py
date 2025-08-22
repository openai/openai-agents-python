"""Integration tests for workflow system."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from agents import Agent, function_tool
from agents.workflow import (
    ConditionalConnection,
    HandoffConnection,
    ParallelConnection,
    SequentialConnection,
    ToolConnection,
    Workflow,
)


class MockResult:
    """Mock RunResult for testing."""

    def __init__(self, output: str, agent: Agent):
        self.final_output = output
        self.last_agent = agent
        self.new_items: list[Any] = []
        self.raw_responses: list[Any] = []
        self.input_guardrail_results: list[Any] = []
        self.output_guardrail_results: list[Any] = []
        self.context_wrapper = None

    def to_input_list(self):
        return [{"role": "assistant", "content": self.final_output}]


class IntegrationTestContext(BaseModel):
    """Context for integration testing."""

    step_count: int = 0
    processed_data: list[str] = []
    routing_decision: str = "default"


@function_tool
def increment_counter(context: IntegrationTestContext) -> str:
    """Increment the step counter."""
    context.step_count += 1
    return f"Counter incremented to {context.step_count}"


@function_tool
def add_data(data: str, context: IntegrationTestContext) -> str:
    """Add data to processed list."""
    context.processed_data.append(data)
    return f"Added data: {data}"


@pytest.mark.asyncio
async def test_workflow_integration_basic():
    """Test basic workflow integration with mocked execution."""
    # Create agents with tools
    agent_1 = Agent[IntegrationTestContext](
        name="Counter Agent",
        instructions="Use the increment tool",
        tools=[increment_counter],
    )

    agent_2 = Agent[IntegrationTestContext](
        name="Data Agent",
        instructions="Use the add_data tool",
        tools=[add_data],
    )

    # Create workflow
    workflow = Workflow[IntegrationTestContext](
        connections=[
            ToolConnection(
                from_agent=agent_1,
                to_agent=agent_2,
                tool_name="process_data",
                tool_description="Process data with agent 2",
            )
        ],
        context=IntegrationTestContext(),
        trace_workflow=False,
    )

    # Mock execution
    from agents.result import RunResult
    
    mock_result = MockResult("Integration test result", agent_2)

    with patch("agents.workflow.connections.Runner.run") as mock_run:
        mock_run.return_value = mock_result

        result = await workflow.run("Test integration")

        assert isinstance(result.final_result, RunResult)
        assert result.final_result.final_output == "Integration test result"


@pytest.mark.asyncio
async def test_complex_workflow_chain():
    """Test complex workflow with multiple connection types."""
    # Create a chain of agents
    intake = Agent[IntegrationTestContext](name="Intake", instructions="Handle intake")
    processor = Agent[IntegrationTestContext](name="Processor", instructions="Process data")
    analyzer = Agent[IntegrationTestContext](name="Analyzer", instructions="Analyze results")
    reporter = Agent[IntegrationTestContext](name="Reporter", instructions="Generate reports")

    # Create complex workflow
    workflow = Workflow[IntegrationTestContext](
        connections=[
            HandoffConnection(intake, processor),
            ToolConnection(
                from_agent=processor,
                to_agent=analyzer,
                tool_name="analyze",
                tool_description="Analyze processed data",
            ),
            SequentialConnection(
                from_agent=processor,
                to_agent=reporter,
                output_transformer=lambda r: f"Analysis complete: {r.final_output}",
            ),
        ],
        context=IntegrationTestContext(),
        name="Complex Integration Workflow",
        trace_workflow=False,
    )

    # Verify workflow structure
    assert len(workflow.connections) == 3
    assert workflow.start_agent == intake
    assert workflow.end_agent == reporter
    assert workflow.agent_count == 4


@pytest.mark.asyncio
async def test_conditional_routing_integration():
    """Test conditional routing in workflows."""
    primary_agent = Agent[IntegrationTestContext](name="Primary", instructions="Primary processing")
    alternative_agent = Agent[IntegrationTestContext](
        name="Alternative", instructions="Alternative processing"
    )
    final_agent = Agent[IntegrationTestContext](name="Final", instructions="Final processing")

    def routing_condition(context, previous_result):
        return context.context.routing_decision == "primary"

    workflow = Workflow[IntegrationTestContext](
        connections=[
            ConditionalConnection(
                from_agent=primary_agent,  # Starting point
                to_agent=primary_agent,  # If condition True
                alternative_agent=alternative_agent,  # If condition False
                condition=routing_condition,
            ),
            SequentialConnection(primary_agent, final_agent),  # This will fail validation
        ],
        trace_workflow=False,
    )

    # This workflow has invalid chain, so validation should fail
    errors = workflow.validate_chain()
    assert len(errors) > 0


@pytest.mark.asyncio
async def test_parallel_connection_integration():
    """Test parallel connection integration."""
    coordinator = Agent[IntegrationTestContext](name="Coordinator", instructions="Coordinate work")
    worker_1 = Agent[IntegrationTestContext](name="Worker 1", instructions="Do work 1")
    worker_2 = Agent[IntegrationTestContext](name="Worker 2", instructions="Do work 2")
    synthesizer = Agent[IntegrationTestContext](name="Synthesizer", instructions="Combine results")

    # Create workflow with parallel connection
    workflow = Workflow[IntegrationTestContext](
        connections=[
            ParallelConnection(
                from_agent=coordinator,
                to_agent=coordinator,  # Not used in parallel
                parallel_agents=[worker_1, worker_2],
                synthesizer_agent=synthesizer,
                synthesis_template="Combined results: {results}",
            )
        ],
        trace_workflow=False,
    )

    # Mock parallel execution
    mock_worker_1_result = MockResult("Worker 1 result", worker_1)
    mock_worker_2_result = MockResult("Worker 2 result", worker_2)
    mock_synthesis_result = MockResult("Synthesized result", synthesizer)

    with patch("agents.workflow.connections.Runner.run") as mock_run:
        # First two calls are for parallel workers, third is for synthesizer
        mock_run.side_effect = [mock_worker_1_result, mock_worker_2_result, mock_synthesis_result]

        with patch("asyncio.gather") as mock_gather:
            mock_gather.return_value = [mock_worker_1_result, mock_worker_2_result]

            await workflow.run("Test parallel")

            # Verify parallel execution was attempted
            mock_gather.assert_called_once()


@pytest.mark.asyncio
async def test_workflow_step_results_tracking():
    """Test that step results are properly tracked."""
    agent_1 = Agent[IntegrationTestContext](name="Agent 1")
    agent_2 = Agent[IntegrationTestContext](name="Agent 2")
    agent_3 = Agent[IntegrationTestContext](name="Agent 3")

    workflow = Workflow[IntegrationTestContext](
        connections=[
            SequentialConnection(agent_1, agent_2),
            SequentialConnection(agent_2, agent_3),
        ],
        trace_workflow=False,
    )

    # Mock results for each step
    mock_result_1 = MockResult("Step 1", agent_2)
    mock_result_2 = MockResult("Step 2", agent_3)

    with patch("agents.workflow.connections.Runner.run") as mock_run:
        mock_run.side_effect = [mock_result_1, mock_result_2]

        result = await workflow.run("Test input")

        # Verify step results tracking
        assert len(result.step_results) == 2
        assert result.step_results[0].final_output == mock_result_1.final_output
        assert result.step_results[1].final_output == mock_result_2.final_output
        assert result.final_result.final_output == mock_result_2.final_output


@pytest.mark.asyncio
async def test_workflow_context_mutation():
    """Test that context can be mutated during workflow execution."""
    context = IntegrationTestContext(step_count=0)

    agent_1 = Agent[IntegrationTestContext](
        name="Agent 1",
        instructions="Increment counter",
        tools=[increment_counter],
    )

    agent_2 = Agent[IntegrationTestContext](
        name="Agent 2",
        instructions="Add data",
        tools=[add_data],
    )

    workflow = Workflow[IntegrationTestContext](
        connections=[SequentialConnection(agent_1, agent_2)],
        context=context,
        trace_workflow=False,
    )

    # Create mock result that simulates context mutation
    mutated_context = IntegrationTestContext(
        step_count=1,
        processed_data=["test_data"],
    )

    mock_result = MockResult("Final output", agent_2)

    with patch("agents.workflow.connections.Runner.run") as mock_run:
        mock_run.return_value = mock_result

        result = await workflow.run("Test input")

        # Context should be the original context (not from mock result)
        assert result.context == context


@pytest.mark.asyncio
async def test_workflow_empty_connections_list():
    """Test workflow validation with empty connections."""
    workflow = Workflow[IntegrationTestContext](connections=[])

    errors = workflow.validate_chain()
    assert len(errors) > 0
    assert "must have at least one connection" in errors[0]


@pytest.mark.asyncio
async def test_workflow_agent_count_calculation():
    """Test agent count calculation with overlapping agents."""
    agent_1 = Agent[IntegrationTestContext](name="Agent 1")
    agent_2 = Agent[IntegrationTestContext](name="Agent 2")

    # Create workflow where agent_1 appears twice
    workflow = Workflow[IntegrationTestContext](
        connections=[
            SequentialConnection(agent_1, agent_2),
            SequentialConnection(agent_2, agent_1),  # agent_1 appears again
        ]
    )

    # Should count unique agents only
    assert workflow.agent_count == 2
