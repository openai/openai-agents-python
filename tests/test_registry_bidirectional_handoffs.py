"""
Tests for registry-based bidirectional handoff functionality.

This module validates the AgentRegistry and AgentRunner features, ensuring scalable
management of agents with bidirectional handoff capabilities, including parent-child
relationships, workflows, and parallel execution.

Author: Ayesha (github.com/CodeVoyager007)
Date: 2025-08-08
"""

import pytest
import asyncio
from typing import Any, Optional
import weakref

from agents import Agent, AgentContext, RunContextWrapper
from agents.registry import (
    AgentRegistry,
    AgentRunner,
    RunResult,
    create_financial_workflow_registry,
    create_support_workflow_registry,
)   
from    agents.handoffs import create_bidirectional_handoff_workflow


class TestAgentRegistry:
    """Test cases for AgentRegistry functionality."""

    def test_registry_initialization(self):
        """Test that registry initializes correctly."""
        registry = AgentRegistry()
        assert registry.agent_configs == {}
        assert registry.agent_instances == {}  # Weak references dict
        assert registry.parent_child_relationships == {}
        assert registry.workflow_configs == {}

    def test_register_agent(self):
        """Test that agents can be registered correctly."""
        registry = AgentRegistry()
        config = {"instructions": "Test agent instructions"}
        registry.register("TestAgent", config)
        assert "TestAgent" in registry.agent_configs
        assert registry.agent_configs["TestAgent"] == config

    def test_register_agent_with_parent(self):
        """Test that agents can be registered with parent relationships."""
        registry = AgentRegistry()
        registry.register("ParentAgent", {"instructions": "Parent"})
        registry.register("ChildAgent", {"instructions": "Child"}, parent_name="ParentAgent")
        assert "ChildAgent" in registry.parent_child_relationships
        assert registry.parent_child_relationships["ChildAgent"] == "ParentAgent"

    def test_get_agent(self):
        """Test that agents can be retrieved from registry."""
        registry = AgentRegistry()
        registry.register("TestAgent", {"instructions": "Test instructions"})
        agent = registry.get_agent("TestAgent")
        assert agent is not None
        assert agent.name == "TestAgent"
        assert agent.instructions == "Test instructions"
        assert isinstance(registry.agent_instances["TestAgent"], weakref.ReferenceType)

    def test_get_agent_not_found(self):
        """Test that get_agent handles missing agents correctly."""
        registry = AgentRegistry()
        with pytest.raises(KeyError, match="Agent 'MissingAgent' not found"):
            registry.get_agent("MissingAgent", create_if_missing=False)
        agent = registry.get_agent("MissingAgent")  # Default: create_if_missing=True
        assert agent is None

    def test_get_agent_with_parent(self):
        """Test that agents are created with parent relationships."""
        registry = AgentRegistry()
        registry.register("ParentAgent", {"instructions": "Parent"})
        registry.register("ChildAgent", {"instructions": "Child"}, parent_name="ParentAgent")
        parent = registry.get_agent("ParentAgent")
        child = registry.get_agent("ChildAgent")
        assert child.parent == parent  # Using weakref-based parent property

    def test_register_workflow(self):
        """Test that workflows can be registered correctly."""
        registry = AgentRegistry()
        registry.register("Orchestrator", {"instructions": "Orchestrator"})
        registry.register("Agent1", {"instructions": "Agent 1"})
        registry.register("Agent2", {"instructions": "Agent 2"})
        registry.register_workflow(
            workflow_name="test_workflow",
            orchestrator_name="Orchestrator",
            sub_agent_names=["Agent1", "Agent2"],
            enable_return_to_parent=True,
        )
        assert "test_workflow" in registry.workflow_configs
        config = registry.workflow_configs["test_workflow"]
        assert config["orchestrator_name"] == "Orchestrator"
        assert config["sub_agent_names"] == ["Agent1", "Agent2"]
        assert config["enable_return_to_parent"] is True

    def test_get_workflow_agents(self):
        """Test that workflow agents can be retrieved correctly."""
        registry = AgentRegistry()
        registry.register("Orchestrator", {"instructions": "Orchestrator"})
        registry.register("Agent1", {"instructions": "Agent 1"})
        registry.register("Agent2", {"instructions": "Agent 2"})
        registry.register_workflow(
            workflow_name="test_workflow",
            orchestrator_name="Orchestrator",
            sub_agent_names=["Agent1", "Agent2"],
        )
        orchestrator, sub_agents = registry.get_workflow_agents("test_workflow")
        assert orchestrator.name == "Orchestrator"
        assert len(sub_agents) == 2
        assert sub_agents[0].name == "Agent1"
        assert sub_agents[1].name == "Agent2"

    def test_get_workflow_agents_not_found(self):
        """Test that get_workflow_agents handles missing workflows correctly."""
        registry = AgentRegistry()
        with pytest.raises(KeyError, match="Workflow 'missing_workflow' not found"):
            registry.get_workflow_agents("missing_workflow")

    def test_list_agents(self):
        """Test that list_agents returns all registered agent names."""
        registry = AgentRegistry()
        registry.register("Agent1", {"instructions": "Agent 1"})
        registry.register("Agent2", {"instructions": "Agent 2"})
        agents = registry.list_agents()
        assert set(agents) == {"Agent1", "Agent2"}

    def test_list_workflows(self):
        """Test that list_workflows returns all registered workflow names."""
        registry = AgentRegistry()
        registry.register("Orchestrator", {"instructions": "Orchestrator"})
        registry.register("Agent1", {"instructions": "Agent 1"})
        registry.register_workflow(
            workflow_name="workflow1",
            orchestrator_name="Orchestrator",
            sub_agent_names=["Agent1"],
        )
        registry.register_workflow(
            workflow_name="workflow2",
            orchestrator_name="Orchestrator",
            sub_agent_names=["Agent1"],
        )
        workflows = registry.list_workflows()
        assert set(workflows) == {"workflow1", "workflow2"}

    def test_remove_agent(self):
        """Test that agents can be removed from registry."""
        registry = AgentRegistry()
        registry.register("TestAgent", {"instructions": "Test"})
        assert "TestAgent" in registry.agent_configs
        success = registry.remove_agent("TestAgent")
        assert success is True
        assert "TestAgent" not in registry.agent_configs
        assert "TestAgent" not in registry.agent_instances
        success = registry.remove_agent("MissingAgent")
        assert success is False

    def test_remove_workflow(self):
        """Test that workflows can be removed from registry."""
        registry = AgentRegistry()
        registry.register("Orchestrator", {"instructions": "Orchestrator"})
        registry.register("Agent1", {"instructions": "Agent 1"})
        registry.register_workflow(
            workflow_name="test_workflow",
            orchestrator_name="Orchestrator",
            sub_agent_names=["Agent1"],
        )
        assert "test_workflow" in registry.workflow_configs
        success = registry.remove_workflow("test_workflow")
        assert success is True
        assert "test_workflow" not in registry.workflow_configs
        success = registry.remove_workflow("missing_workflow")
        assert success is False

    def test_clear(self):
        """Test that registry can be cleared completely."""
        registry = AgentRegistry()
        registry.register("Agent1", {"instructions": "Agent 1"})
        registry.register("Agent2", {"instructions": "Agent 2"}, parent_name="Agent1")
        registry.register_workflow(
            workflow_name="test_workflow",
            orchestrator_name="Agent1",
            sub_agent_names=["Agent2"],
        )
        assert len(registry.agent_configs) == 2
        assert len(registry.workflow_configs) == 1
        registry.clear()
        assert len(registry.agent_configs) == 0
        assert len(registry.agent_instances) == 0
        assert len(registry.parent_child_relationships) == 0
        assert len(registry.workflow_configs) == 0

    def test_to_dict(self):
        """Test that registry can be serialized to dictionary."""
        registry = AgentRegistry()
        registry.register("Agent1", {"instructions": "Agent 1"})
        registry.register("Agent2", {"instructions": "Agent 2"}, parent_name="Agent1")
        registry.register_workflow(
            workflow_name="test_workflow",
            orchestrator_name="Agent1",
            sub_agent_names=["Agent2"],
        )
        data = registry.to_dict()
        assert "registered_agents" in data
        assert "active_instances" in data
        assert "parent_child_relationships" in data
        assert "workflow_configs" in data
        assert data["registered_agents"]["Agent1"] == {"instructions": "Agent 1"}
        assert "test_workflow" in data["workflow_configs"]


class TestAgentRunner:
    """Test cases for AgentRunner functionality."""

    def test_runner_initialization(self):
        """Test that AgentRunner initializes correctly."""
        registry = AgentRegistry()
        runner = AgentRunner(registry, max_turns=15, max_concurrent=3)
        assert runner.registry == registry
        assert runner.max_turns == 15
        assert runner.max_concurrent == 3

    @pytest.mark.asyncio
    async def test_run_with_registry(self):
        """Test that AgentRunner can run workflows using registry."""
        registry = AgentRegistry()
        registry.register("TestAgent", {"instructions": "Test agent"})
        runner = AgentRunner(registry)
        context = AgentContext()
        result = await runner.run(
            entry_agent_name="TestAgent",
            input_task="Test task",
            context=context,
            max_turns=5,
        )
        assert isinstance(result, RunResult)
        assert result.turn_count >= 0
        assert "TestAgent" in result.agent_history
        assert result.context == context

    @pytest.mark.asyncio
    async def test_run_workflow(self):
        """Test that AgentRunner can run registered workflows."""
        registry = AgentRegistry()
        registry.register("Orchestrator", {"instructions": "Orchestrator"})
        registry.register("Agent1", {"instructions": "Agent 1"}, parent_name="Orchestrator")
        registry.register_workflow(
            workflow_name="test_workflow",
            orchestrator_name="Orchestrator",
            sub_agent_names=["Agent1"],
            enable_return_to_parent=True,
        )
        runner = AgentRunner(registry)
        result = await runner.run_workflow(
            workflow_name="test_workflow",
            input_task="Test workflow task",
            context=AgentContext(),
            max_turns=5,
        )
        assert isinstance(result, RunResult)
        assert result.turn_count >= 0
        assert "Orchestrator" in result.agent_history
        assert "Agent1" in result.agent_history

    @pytest.mark.asyncio
    async def test_run_parallel(self):
        """Test that AgentRunner can run tasks in parallel."""
        registry = AgentRegistry()
        registry.register("Agent1", {"instructions": "Agent 1"})
        registry.register("Agent2", {"instructions": "Agent 2"})
        runner = AgentRunner(registry)
        agent_tasks = [
            ("Agent1", "Task 1"),
            ("Agent2", "Task 2"),
        ]
        results = await runner.run_parallel(
            agent_tasks=agent_tasks,
            context=AgentContext(),
            max_turns=3,
        )
        assert len(results) == 2
        assert all(isinstance(result, RunResult) for result in results)
        assert any("Agent1" in result.agent_history for result in results)
        assert any("Agent2" in result.agent_history for result in results)

    @pytest.mark.asyncio
    async def test_workflow_session(self):
        """Test that AgentRunner can use workflow sessions."""
        registry = AgentRegistry()
        registry.register("Orchestrator", {"instructions": "Orchestrator"})
        registry.register("Agent1", {"instructions": "Agent 1"})
        registry.register_workflow(
            workflow_name="test_workflow",
            orchestrator_name="Orchestrator",
            sub_agent_names=["Agent1"],
        )
        runner = AgentRunner(registry)
        async with runner.workflow_session("test_workflow") as session_runner:
            result = await session_runner.run_workflow(
                workflow_name="test_workflow",
                input_task="Session task",
                context=AgentContext(),
                max_turns=3,
            )
            assert isinstance(result, RunResult)


class TestConvenienceFunctions:
    """Test cases for convenience functions."""

    def test_create_financial_workflow_registry(self):
        """Test that financial workflow registry is created correctly."""
        registry = create_financial_workflow_registry()
        agents = registry.list_agents()
        assert {"Orchestrator", "FinancialAgent", "DocsAgent"}.issubset(agents)
        workflows = registry.list_workflows()
        assert "financial_research" in workflows
        assert registry.parent_child_relationships["FinancialAgent"] == "Orchestrator"
        assert registry.parent_child_relationships["DocsAgent"] == "Orchestrator"

    def test_create_support_workflow_registry(self):
        """Test that support workflow registry is created correctly."""
        registry = create_support_workflow_registry()
        agents = registry.list_agents()
        assert {"TriageAgent", "BillingAgent", "TechnicalAgent"}.issubset(agents)
        workflows = registry.list_workflows()
        assert "customer_support" in workflows
        assert registry.parent_child_relationships["BillingAgent"] == "TriageAgent"
        assert registry.parent_child_relationships["TechnicalAgent"] == "TriageAgent"


class TestRunResult:
    """Test cases for RunResult class."""

    def test_run_result_creation(self):
        """Test that RunResult can be created correctly."""
        result = RunResult(
            final_output="Test output",
            intermediate_results=["Result 1", "Result 2"],
            turn_count=5,
            agent_history=["Agent1", "Agent2"],
            context=AgentContext(),
        )
        assert result.final_output == "Test output"
        assert len(result.intermediate_results) == 2
        assert result.turn_count == 5
        assert len(result.agent_history) == 2
        assert isinstance(result.context, AgentContext)

    def test_run_result_to_dict(self):
        """Test that RunResult can be serialized to dictionary."""
        result = RunResult(
            final_output="Test output",
            intermediate_results=["Result 1", "Result 2"],
            turn_count=5,
            agent_history=["Agent1", "Agent2"],
        )
        data = result.to_dict()
        assert data["final_output"] == "Test output"
        assert len(data["intermediate_results"]) == 2
        assert data["turn_count"] == 5
        assert len(data["agent_history"]) == 2
        assert data["error"] is None

    def test_run_result_with_error(self):
        """Test that RunResult handles errors correctly."""
        result = RunResult(
            final_output=None,
            error="Test error message",
            turn_count=0,
            context=AgentContext(),
        )
        assert result.final_output is None
        assert result.error == "Test error message"
        assert result.turn_count == 0
        assert isinstance(result.context, AgentContext)


class TestRegistryIntegration:
    """Test cases for registry integration with bidirectional handoffs."""

    def test_registry_with_bidirectional_handoffs(self):
        """Test that registry works with bidirectional handoffs."""
        registry = AgentRegistry()
        registry.register("Orchestrator", {"instructions": "Orchestrator"})
        registry.register("Agent1", {"instructions": "Agent 1"}, parent_name="Orchestrator")
        registry.register("Agent2", {"instructions": "Agent 2"}, parent_name="Orchestrator")
        orchestrator = registry.get_agent("Orchestrator")
        agent1 = registry.get_agent("Agent1")
        agent2 = registry.get_agent("Agent2")
        orchestrator, sub_agents = create_bidirectional_handoff_workflow(
            orchestrator_agent=orchestrator,
            sub_agents=[agent1, agent2],
            enable_return_to_parent=True,
        )
        assert agent1.parent == orchestrator
        assert agent2.parent == orchestrator
        assert len(orchestrator.handoffs) == 2
        assert len(agent1.handoffs) == 1
        assert agent1.handoffs[0].is_return_to_parent is True
        assert len(agent2.handoffs) == 1
        assert agent2.handoffs[0].is_return_to_parent is True

    @pytest.mark.asyncio
    async def test_registry_workflow_with_bidirectional_handoffs(self):
        """Test that registry workflows work with bidirectional handoffs."""
        registry = AgentRegistry()
        registry.register("Orchestrator", {"instructions": "Orchestrator"})
        registry.register("Agent1", {"instructions": "Agent 1"}, parent_name="Orchestrator")
        registry.register("Agent2", {"instructions": "Agent 2"}, parent_name="Orchestrator")
        registry.register_workflow(
            workflow_name="test_workflow",
            orchestrator_name="Orchestrator",
            sub_agent_names=["Agent1", "Agent2"],
            enable_return_to_parent=True,
        )
        runner = AgentRunner(registry)
        result = await runner.run_workflow(
            workflow_name="test_workflow",
            input_task="Test bidirectional workflow",
            context=AgentContext(),
            max_turns=5,
        )
        assert isinstance(result, RunResult)
        assert "Orchestrator" in result.agent_history
        assert any(agent in result.agent_history for agent in ["Agent1", "Agent2"])
        orchestrator, sub_agents = registry.get_workflow_agents("test_workflow")
        assert sub_agents[0].parent == orchestrator
        assert sub_agents[1].parent == orchestrator
        assert orchestrator.can_return_to_parent() is False
        assert sub_agents[0].can_return_to_parent() is True
        assert sub_agents[1].can_return_to_parent() is True