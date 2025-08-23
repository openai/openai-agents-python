"""Tests for workflow examples to ensure they work correctly."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agents import Agent, function_tool
from agents.workflow import HandoffConnection, SequentialConnection, ToolConnection, Workflow

from .conftest import TestContext


@function_tool(strict_mode=False)
def increment_counter(context: TestContext) -> str:
    """Increment the step counter."""
    context.counter += 1
    return f"Counter incremented to {context.counter}"


@function_tool(strict_mode=False)
def add_data(data: str, context: TestContext) -> str:
    """Add data to processed list."""
    # TestContext doesn't have processed_data, so we'll just return a message
    return f"Added data: {data}"


@pytest.mark.asyncio
async def test_basic_workflow_example_structure():
    """Test that the basic workflow example has correct structure."""
    # This tests the pattern from examples/workflows/basic_workflow.py

    triage_agent = Agent[TestContext](
        name="Triage Agent",
        instructions="Route requests to specialists",
        handoff_description="Routes requests to appropriate specialists",
    )

    content_agent = Agent[TestContext](
        name="Content Agent",
        instructions="Process content requests",
        handoff_description="Processes content requests",
    )

    analysis_agent = Agent[TestContext](
        name="Analysis Agent",
        instructions="Provide analysis",
        handoff_description="Provides detailed analysis and insights",
    )

    summary_agent = Agent[TestContext](
        name="Summary Agent",
        instructions="Create summaries",
        handoff_description="Creates final summaries",
    )

    # Create workflow following the example pattern
    workflow = Workflow[TestContext](
        connections=[
            HandoffConnection(
                from_agent=triage_agent,
                to_agent=content_agent,
                tool_description_override="Transfer to content specialist for processing",
            ),
            ToolConnection(
                from_agent=content_agent,
                to_agent=analysis_agent,
                tool_name="get_analysis",
                tool_description="Get detailed analysis and insights",
            ),
            SequentialConnection(
                from_agent=content_agent,
                to_agent=summary_agent,
                output_transformer=lambda result: (
                    f"Previous conversation and analysis:\n{result.final_output}"
                ),
            ),
        ],
        name="Content Processing Workflow",
        context=TestContext(),
        trace_workflow=True,
    )

    # Validate structure
    assert len(workflow.connections) == 3
    assert workflow.name == "Content Processing Workflow"
    assert workflow.start_agent.name == "Triage Agent"
    assert workflow.end_agent.name == "Summary Agent"

    # Validate chain
    errors = workflow.validate_chain()
    assert len(errors) == 0


@pytest.mark.asyncio
async def test_workflow_example_patterns():
    """Test various workflow patterns from examples."""

    # Pattern 1: Simple handoff chain
    agent_a = Agent[TestContext](name="Agent A")
    agent_b = Agent[TestContext](name="Agent B")
    agent_c = Agent[TestContext](name="Agent C")

    handoff_workflow = Workflow[TestContext](
        connections=[
            HandoffConnection(agent_a, agent_b),
            HandoffConnection(agent_b, agent_c),
        ]
    )

    assert handoff_workflow.agent_count == 3
    errors = handoff_workflow.validate_chain()
    assert len(errors) == 0

    # Pattern 2: Tool chain
    tool_workflow = Workflow[TestContext](
        connections=[
            ToolConnection(agent_a, agent_b, tool_name="tool_b"),
            ToolConnection(agent_a, agent_c, tool_name="tool_c"),  # This breaks the chain
        ]
    )

    # This should have no validation errors with flexible validation
    errors = tool_workflow.validate_chain()
    assert len(errors) == 0

    # Pattern 3: Sequential processing
    sequential_workflow = Workflow[TestContext](
        connections=[
            SequentialConnection(agent_a, agent_b),
            SequentialConnection(agent_b, agent_c),
        ]
    )

    assert sequential_workflow.agent_count == 3
    errors = sequential_workflow.validate_chain()
    assert len(errors) == 0


@pytest.mark.asyncio
async def test_workflow_with_context_tools():
    """Test workflow with agents that use context-aware tools."""

    counter_agent = Agent[TestContext](
        name="Counter Agent",
        instructions="Use tools to modify context",
        tools=[increment_counter],
    )

    data_agent = Agent[TestContext](
        name="Data Agent",
        instructions="Add data to context",
        tools=[add_data],
    )

    workflow = Workflow[TestContext](
        connections=[SequentialConnection(counter_agent, data_agent)],
        context=TestContext(),
    )

    # Verify agents have the expected tools
    assert len(counter_agent.tools) == 1
    assert len(data_agent.tools) == 1

    # Verify workflow structure
    errors = workflow.validate_chain()
    assert len(errors) == 0


@pytest.mark.asyncio
async def test_workflow_cloning_preserves_structure():
    """Test that workflow cloning preserves all structural elements."""
    agent_1 = Agent[TestContext](name="Agent 1")
    agent_2 = Agent[TestContext](name="Agent 2")

    original = Workflow[TestContext](
        connections=[
            HandoffConnection(
                from_agent=agent_1,
                to_agent=agent_2,
                tool_description_override="Custom description",
            )
        ],
        name="Original Workflow",
        max_steps=15,
        trace_workflow=False,
        context=TestContext(test_data="original"),
    )

    # Clone with modifications
    cloned = original.clone(
        name="Cloned Workflow",
        max_steps=25,
        trace_workflow=True,
    )

    # Verify cloning preserved structure
    assert len(cloned.connections) == 1
    assert cloned.connections[0].from_agent == agent_1
    assert cloned.connections[0].to_agent == agent_2
    # Check if it's a HandoffConnection to access tool_description_override
    if hasattr(cloned.connections[0], "tool_description_override"):
        assert cloned.connections[0].tool_description_override == "Custom description"

    # Verify modifications applied
    assert cloned.name == "Cloned Workflow"
    assert cloned.max_steps == 25
    assert cloned.trace_workflow is True

    # Verify original unchanged
    assert original.name == "Original Workflow"
    assert original.max_steps == 15
    assert original.trace_workflow is False


@pytest.mark.asyncio
async def test_workflow_add_connection_chain_validation():
    """Test that adding connections maintains chain validation."""
    agent_1 = Agent[TestContext](name="Agent 1")
    agent_2 = Agent[TestContext](name="Agent 2")
    agent_3 = Agent[TestContext](name="Agent 3")

    # Start with valid workflow
    workflow = Workflow[TestContext](connections=[SequentialConnection(agent_1, agent_2)])

    # Add valid connection
    extended = workflow.add_connection(SequentialConnection(agent_2, agent_3))
    errors = extended.validate_chain()
    assert len(errors) == 0

    # Add invalid connection (breaks chain)
    broken = workflow.add_connection(SequentialConnection(agent_3, agent_1))
    errors = broken.validate_chain()
    assert len(errors) == 0  # No validation errors with flexible validation


@pytest.mark.asyncio
async def test_workflow_execution_order():
    """Test that workflow executes connections in correct order."""
    agent_1 = Agent[TestContext](name="Agent 1")
    agent_2 = Agent[TestContext](name="Agent 2")
    agent_3 = Agent[TestContext](name="Agent 3")

    workflow = Workflow[TestContext](
        connections=[
            SequentialConnection(agent_1, agent_2),
            SequentialConnection(agent_2, agent_3),
        ],
        trace_workflow=False,
    )

    call_order = []

    def track_calls(*args, **kwargs):
        # Track which agent is being called
        agent = kwargs.get("starting_agent") or args[0]
        call_order.append(agent.name)

        # Return mock result
        from agents.result import RunResult
        from agents.run_context import RunContextWrapper

        return RunResult(
            input="test input",
            new_items=[],
            raw_responses=[],
            final_output=f"Output from {agent.name}",
            input_guardrail_results=[],
            output_guardrail_results=[],
            context_wrapper=RunContextWrapper(TestContext()),
            _last_agent=agent,
        )

    with patch("agents.run.Runner.run", side_effect=track_calls):
        await workflow.run("Test input")

        # Should execute connections in order
        # First connection: agent_1 -> agent_2 (executes agent_1, then agent_2)
        # Second connection: agent_2 -> agent_3 (executes agent_2, then agent_3)
        assert len(call_order) == 4
        assert call_order == ["Agent 1", "Agent 2", "Agent 2", "Agent 3"]
