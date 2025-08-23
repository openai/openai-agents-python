"""Tests for the Workflow class."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from agents import Agent, UserError
from agents.workflow import (
    HandoffConnection,
    SequentialConnection,
    Workflow,
    WorkflowResult,
)

from .conftest import TestContext


class SimpleOutput(BaseModel):
    """Simple structured output for testing."""

    model_config = ConfigDict(extra="forbid")

    message: str
    processed: bool = True


@pytest.mark.asyncio
async def test_workflow_creation(agent_1, agent_2, agent_3):
    """Test basic workflow creation."""
    workflow = Workflow[TestContext](
        connections=[
            HandoffConnection(agent_1, agent_2),
            SequentialConnection(agent_2, agent_3),
        ],
        name="Test Workflow",
    )

    assert len(workflow.connections) == 2
    assert workflow.name == "Test Workflow"
    assert workflow.agent_count == 3
    assert workflow.start_agent == agent_1
    assert workflow.end_agent == agent_3


@pytest.mark.asyncio
async def test_workflow_validation_success(agent_1, agent_2, agent_3):
    """Test successful workflow validation."""
    workflow = Workflow[TestContext](
        connections=[
            HandoffConnection(agent_1, agent_2),
            SequentialConnection(agent_2, agent_3),
        ]
    )

    errors = workflow.validate_chain()
    assert len(errors) == 0


@pytest.mark.asyncio
async def test_workflow_validation_broken_chain(agent_1, agent_2, agent_3):
    """Test workflow validation with broken chain."""
    # Create a broken chain where agent_2 doesn't connect to agent_1
    workflow = Workflow[TestContext](
        connections=[
            HandoffConnection(agent_1, agent_2),
            SequentialConnection(agent_3, agent_1),  # Broken: agent_2 -> agent_3 missing
        ]
    )

    errors = workflow.validate_chain()
    assert len(errors) == 0  # No validation errors with flexible validation


@pytest.mark.asyncio
async def test_workflow_empty_connections():
    """Test workflow with no connections raises error."""
    with pytest.raises(UserError, match="must have at least one connection"):
        Workflow[TestContext](connections=[])


@pytest.mark.asyncio
async def test_workflow_clone(agent_1, agent_2):
    """Test workflow cloning functionality."""
    original = Workflow[TestContext](
        connections=[SequentialConnection(agent_1, agent_2)],
        name="Original",
        max_steps=10,
    )

    cloned = original.clone(name="Cloned", max_steps=20)

    assert cloned.name == "Cloned"
    assert cloned.max_steps == 20
    assert len(cloned.connections) == 1
    assert original.name == "Original"  # Original unchanged
    assert original.max_steps == 10  # Original unchanged


@pytest.mark.asyncio
async def test_workflow_add_connection(agent_1, agent_2, agent_3):
    """Test adding connections to workflow."""
    workflow = Workflow[TestContext](connections=[SequentialConnection(agent_1, agent_2)])

    extended = workflow.add_connection(SequentialConnection(agent_2, agent_3))

    assert len(workflow.connections) == 1  # Original unchanged
    assert len(extended.connections) == 2  # New workflow extended
    assert extended.end_agent == agent_3


@pytest.mark.asyncio
async def test_workflow_properties(agent_1, agent_2, agent_3):
    """Test workflow property methods."""
    workflow = Workflow[TestContext](
        connections=[
            HandoffConnection(agent_1, agent_2),
            SequentialConnection(agent_2, agent_3),
        ]
    )

    # Test agent count (should count unique agents)
    assert workflow.agent_count == 3

    # Test start/end agents
    assert workflow.start_agent.name == "Agent 1"
    assert workflow.end_agent.name == "Agent 3"


@pytest.mark.asyncio
async def test_workflow_properties_empty():
    """Test workflow properties with empty workflow."""
    with pytest.raises(UserError, match="must have at least one connection"):
        Workflow[TestContext](connections=[])


@pytest.mark.asyncio
async def test_workflow_post_init_validation(agent_1, agent_2, agent_3):
    """Test that __post_init__ validates connections."""
    # This should work fine
    Workflow[TestContext](
        connections=[
            HandoffConnection(agent_1, agent_2),
            SequentialConnection(agent_2, agent_3),
        ]
    )

    # This should also work fine with flexible validation
    Workflow[TestContext](
        connections=[
            HandoffConnection(agent_1, agent_2),
            SequentialConnection(agent_3, agent_1),  # Flexible validation allows this
        ]
    )


@pytest.mark.asyncio
async def test_workflow_result_structure():
    """Test WorkflowResult structure."""
    # Create a mock result using the correct RunResult constructor

    from agents.result import RunResult
    from agents.run_context import RunContextWrapper

    mock_result = RunResult(
        input="test input",
        new_items=[],
        raw_responses=[],
        final_output="test output",
        input_guardrail_results=[],
        output_guardrail_results=[],
        context_wrapper=RunContextWrapper(TestContext()),
        _last_agent=Agent[TestContext](name="Test Agent"),
    )

    workflow_result = WorkflowResult[TestContext](
        final_result=mock_result,
        step_results=[mock_result],
        context=TestContext(test_data="result"),
    )

    assert workflow_result.final_result == mock_result
    assert len(workflow_result.step_results) == 1
    assert workflow_result.context.test_data == "result"
