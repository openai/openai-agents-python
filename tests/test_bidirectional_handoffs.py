"""
Tests for bidirectional handoff functionality with registry support.

This module tests the bidirectional handoff features, including parent-child relationships,
return-to-parent handoffs, orchestrator workflows, and registry-based agent management.

Author: Ayesha (github.com/CodeVoyager007)
Date: 2025-08-08
"""

from agents.run_context import RunContextWrapper
import pytest
import asyncio
from typing import Any
from agents import Agent, AgentContext, Runner
from agents.handoffs import (
    create_bidirectional_handoff_workflow,
    return_to_parent_handoff,
    handoff,
)
from agents.registry import AgentRegistry, AgentRunner, RunResult


class TestBidirectionalHandoffs:
    """Test cases for bidirectional handoff functionality."""

    def test_agent_parent_relationship(self):
        """Test that parent-child relationships can be set up correctly."""
        registry = AgentRegistry()
        registry.register("Parent", {"instructions": "Parent agent"})
        registry.register("Child", {"instructions": "Child agent"}, parent_name="Parent")

        parent = registry.get_agent("Parent")
        child = registry.get_agent("Child")

        # Test setting parent
        child.set_parent(parent)
        assert child.parent == parent  # Using weakref-based parent property
        assert child.can_return_to_parent() is True

        # Test without parent
        orphan = Agent(name="Orphan", instructions="Orphan agent")
        assert orphan.can_return_to_parent() is False

    def test_return_to_parent_handoff_creation(self):
        """Test that return_to_parent_handoff creates correct handoff."""
        registry = AgentRegistry()
        registry.register("Parent", {"instructions": "Parent agent"})
        parent = registry.get_agent("Parent")
        handoff_obj = return_to_parent_handoff(parent)

        assert handoff_obj.tool_name == "return_to_parent"
        assert "Return control to the parent agent" in handoff_obj.tool_description
        assert handoff_obj.is_return_to_parent is True
        assert handoff_obj.agent_name == "Parent"

    def test_create_bidirectional_workflow(self):
        """Test that create_bidirectional_handoff_workflow sets up relationships correctly."""
        registry = AgentRegistry()
        registry.register("Orchestrator", {"instructions": "Orchestrator"})
        registry.register("Agent1", {"instructions": "Agent 1"}, parent_name="Orchestrator")
        registry.register("Agent2", {"instructions": "Agent 2"}, parent_name="Orchestrator")

        orchestrator = registry.get_agent("Orchestrator")
        agent1 = registry.get_agent("Agent1")
        agent2 = registry.get_agent("Agent2")

        result_orchestrator, result_agents = create_bidirectional_handoff_workflow(
            orchestrator_agent=orchestrator,
            sub_agents=[agent1, agent2],
            enable_return_to_parent=True,
        )

        # Check that parent relationships are set
        assert agent1.parent == orchestrator
        assert agent2.parent == orchestrator

        # Check that orchestrator has handoffs to sub-agents
        assert len(result_orchestrator.handoffs) == 2
        handoff_names = [h.agent_name for h in result_orchestrator.handoffs]
        assert "Agent1" in handoff_names
        assert "Agent2" in handoff_names

        # Check that sub-agents have return-to-parent handoffs
        assert len(agent1.handoffs) == 1
        assert agent1.handoffs[0].is_return_to_parent is True
        assert len(agent2.handoffs) == 1
        assert agent2.handoffs[0].is_return_to_parent is True

    def test_create_bidirectional_workflow_disabled(self):
        """Test that create_bidirectional_handoff_workflow works when return_to_parent is disabled."""
        registry = AgentRegistry()
        registry.register("Orchestrator", {"instructions": "Orchestrator"})
        registry.register("Agent1", {"instructions": "Agent 1"})

        orchestrator = registry.get_agent("Orchestrator")
        agent1 = registry.get_agent("Agent1")

        result_orchestrator, result_agents = create_bidirectional_handoff_workflow(
            orchestrator_agent=orchestrator,
            sub_agents=[agent1],
            enable_return_to_parent=False,
        )

        # Check that parent relationships are NOT set
        assert agent1.parent is None

        # Check that orchestrator has handoffs to sub-agents
        assert len(result_orchestrator.handoffs) == 1

        # Check that sub-agents do NOT have return-to-parent handoffs
        assert len(agent1.handoffs) == 0

    def test_agent_clone_preserves_parent(self):
        """Test that cloning an agent preserves the parent relationship."""
        registry = AgentRegistry()
        registry.register("Parent", {"instructions": "Parent"})
        registry.register("Child", {"instructions": "Child"}, parent_name="Parent")

        parent = registry.get_agent("Parent")
        child = registry.get_agent("Child")
        child.set_parent(parent)

        cloned_child = child.clone(instructions="Cloned child")
        assert cloned_child.parent == parent
        assert cloned_child.can_return_to_parent() is True

    def test_return_to_parent_enabled_flag(self):
        """Test that return_to_parent_enabled flag works correctly."""
        registry = AgentRegistry()
        registry.register("Parent", {"instructions": "Parent"})
        registry.register("Child", {"instructions": "Child"}, parent_name="Parent")

        parent = registry.get_agent("Parent")
        child = registry.get_agent("Child")
        child.set_parent(parent)

        # Test with flag enabled
        child.return_to_parent_enabled = True
        assert child.can_return_to_parent() is True

        # Test with flag disabled
        child.return_to_parent_enabled = False
        assert child.can_return_to_parent() is False

        # Test with no parent
        child.parent = None
        child.return_to_parent_enabled = True
        assert child.can_return_to_parent() is False

    def test_handoff_with_custom_tool_name(self):
        """Test that return_to_parent_handoff accepts custom tool names."""
        registry = AgentRegistry()
        registry.register("Parent", {"instructions": "Parent"})
        parent = registry.get_agent("Parent")
        handoff_obj = return_to_parent_handoff(
            parent,
            tool_name_override="custom_return_tool",
            tool_description_override="Custom return description",
        )

        assert handoff_obj.tool_name == "custom_return_tool"
        assert handoff_obj.tool_description == "Custom return description"

    def test_handoff_enabled_function(self):
        """Test that return_to_parent_handoff works with enabled function."""
        registry = AgentRegistry()
        registry.register("Parent", {"instructions": "Parent"})
        parent = registry.get_agent("Parent")

        def is_enabled(ctx, agent):
            return agent.name == "Child"

        handoff_obj = return_to_parent_handoff(
            parent,
            is_enabled=is_enabled,
        )

        assert callable(handoff_obj.is_enabled)

    def test_multiple_parents_not_allowed(self):
        """Test that an agent can only have one parent at a time."""
        registry = AgentRegistry()
        registry.register("Parent1", {"instructions": "Parent 1"})
        registry.register("Parent2", {"instructions": "Parent 2"})
        registry.register("Child", {"instructions": "Child"})

        parent1 = registry.get_agent("Parent1")
        parent2 = registry.get_agent("Parent2")
        child = registry.get_agent("Child")

        # Set first parent
        child.set_parent(parent1)
        assert child.parent == parent1

        # Set second parent (should replace first)
        child.set_parent(parent2)
        assert child.parent == parent2
        assert child.parent != parent1

    @pytest.mark.asyncio
    async def test_handoff_invocation_returns_parent(self):
        """Test that the handoff invocation actually returns the parent agent."""
        registry = AgentRegistry()
        registry.register("Parent", {"instructions": "Parent"})
        parent = registry.get_agent("Parent")
        handoff_obj = return_to_parent_handoff(parent)

        # Mock context
        class MockContext(RunContextWrapper):
            def __init__(self):
                super().__init__(AgentContext())
                self.agent = None

        ctx = MockContext()

        # Test the handoff invocation
        result = await handoff_obj.on_invoke_handoff(ctx, None)
        assert result == parent

    @pytest.mark.asyncio
    async def test_bidirectional_workflow_integration(self):
        """Test that bidirectional workflows integrate with AgentRunner."""
        registry = AgentRegistry()
        registry.register("Orchestrator", {"instructions": "Coordinate tasks by handing off to specialized agents."})
        registry.register("Agent1", {"instructions": "Complete task and return to parent."}, parent_name="Orchestrator")
        registry.register("Agent2", {"instructions": "Complete task and return to parent."}, parent_name="Orchestrator")

        orchestrator = registry.get_agent("Orchestrator")
        agent1 = registry.get_agent("Agent1")
        agent2 = registry.get_agent("Agent2")

        # Set up bidirectional workflow
        orchestrator, sub_agents = create_bidirectional_handoff_workflow(
            orchestrator_agent=orchestrator,
            sub_agents=[agent1, agent2],
            enable_return_to_parent=True,
        )

        runner = AgentRunner(registry)
        result = await runner.run(
            entry_agent_name="Orchestrator",
            input_task="Test task",
            context=AgentContext(),
            max_turns=5
        )

        assert result.final_output is not None
        assert len(result.intermediate_results) > 0
        assert "Orchestrator processed" in str(result.final_output)
        assert "Agent1" in result.agent_history or "Agent2" in result.agent_history

    def test_manual_setup_equivalent_to_helper(self):
        """Test that manual setup produces the same result as the helper function."""
        registry = AgentRegistry()
        registry.register("Parent", {"instructions": "Parent"})
        registry.register("Child1", {"instructions": "Child 1"}, parent_name="Parent")
        registry.register("Child2", {"instructions": "Child 2"}, parent_name="Parent")

        # Manual setup
        parent_manual = registry.get_agent("Parent")
        child1_manual = registry.get_agent("Child1")
        child2_manual = registry.get_agent("Child2")

        child1_manual.set_parent(parent_manual)
        child2_manual.set_parent(parent_manual)

        parent_manual.handoffs.extend([
            handoff(child1_manual),
            handoff(child2_manual),
        ])

        child1_manual.handoffs.append(return_to_parent_handoff(parent_manual))
        child2_manual.handoffs.append(return_to_parent_handoff(parent_manual))

        # Helper function setup
        parent_helper = registry.get_agent("Parent")
        child1_helper = registry.get_agent("Child1")
        child2_helper = registry.get_agent("Child2")

        parent_helper, sub_agents_helper = create_bidirectional_handoff_workflow(
            orchestrator_agent=parent_helper,
            sub_agents=[child1_helper, child2_helper],
            enable_return_to_parent=True,
        )

        # Compare results
        assert child1_manual.parent == child1_helper.parent
        assert child2_manual.parent == child2_helper.parent
        assert len(parent_manual.handoffs) == len(parent_helper.handoffs)
        assert len(child1_manual.handoffs) == len(child1_helper.handoffs)
        assert len(child2_manual.handoffs) == len(child2_helper.handoffs)

    def test_handoff_with_input_filter(self):
        """Test that return_to_parent_handoff works with input filters."""
        registry = AgentRegistry()
        registry.register("Parent", {"instructions": "Parent"})
        parent = registry.get_agent("Parent")

        def input_filter(handoff_data):
            # Only pass essential information back to parent
            return handoff_data.clone(new_items=())

        handoff_obj = return_to_parent_handoff(
            parent,
            input_filter=input_filter,
        )

        assert handoff_obj.input_filter == input_filter


class TestBidirectionalHandoffEdgeCases:
    """Test edge cases and error conditions for bidirectional handoffs."""

    def test_empty_sub_agents_list(self):
        """Test that create_bidirectional_handoff_workflow handles empty sub-agents list."""
        registry = AgentRegistry()
        registry.register("Orchestrator", {"instructions": "Orchestrator"})
        orchestrator = registry.get_agent("Orchestrator")
        result_orchestrator, result_agents = create_bidirectional_handoff_workflow(
            orchestrator_agent=orchestrator,
            sub_agents=[],
            enable_return_to_parent=True,
        )

        assert len(result_orchestrator.handoffs) == 0
        assert result_agents == []

    def test_none_parent_agent(self):
        """Test that return_to_parent_handoff handles None parent gracefully."""
        with pytest.raises(AttributeError):
            return_to_parent_handoff(None)

    def test_circular_parent_references(self):
        """Test that circular parent references are handled correctly."""
        registry = AgentRegistry()
        registry.register("Agent1", {"instructions": "Agent 1"})
        registry.register("Agent2", {"instructions": "Agent 2"})

        agent1 = registry.get_agent("Agent1")
        agent2 = registry.get_agent("Agent2")

        # Set up circular reference
        agent1.set_parent(agent2)
        agent2.set_parent(agent1)

        # This should not cause infinite recursion
        assert agent1.parent == agent2
        assert agent2.parent == agent1

    def test_handoff_with_disabled_return_to_parent(self):
        """Test that agents with disabled return_to_parent still work."""
        registry = AgentRegistry()
        registry.register("Parent", {"instructions": "Parent"})
        registry.register("Child", {"instructions": "Child"}, parent_name="Parent")

        parent = registry.get_agent("Parent")
        child = registry.get_agent("Child")
        child.set_parent(parent)
        child.return_to_parent_enabled = False

        assert child.parent == parent
        assert child.can_return_to_parent() is False

    def test_multiple_return_to_parent_handoffs(self):
        """Test that multiple return_to_parent handoffs work correctly."""
        registry = AgentRegistry()
        registry.register("Parent", {"instructions": "Parent"})
        registry.register("Child", {"instructions": "Child"}, parent_name="Parent")

        parent = registry.get_agent("Parent")
        child = registry.get_agent("Child")
        child.set_parent(parent)

        handoff1 = return_to_parent_handoff(parent, tool_name_override="return1")
        handoff2 = return_to_parent_handoff(parent, tool_name_override="return2")

        child.handoffs.extend([handoff1, handoff2])

        assert len(child.handoffs) == 2
        assert child.handoffs[0].tool_name == "return1"
        assert child.handoffs[1].tool_name == "return2"
        assert child.handoffs[0].is_return_to_parent is True
        assert child.handoffs[1].is_return_to_parent is True

    @pytest.mark.asyncio
    async def test_registry_workflow_execution(self):
        """Test registry-based workflow execution."""
        registry = AgentRegistry()
        registry.register("Orchestrator", {"instructions": "Coordinate tasks"})
        registry.register("Agent1", {"instructions": "Task 1"}, parent_name="Orchestrator")
        registry.register("Agent2", {"instructions": "Task 2"}, parent_name="Orchestrator")

        registry.register_workflow(
            workflow_name="test_workflow",
            orchestrator_name="Orchestrator",
            sub_agent_names=["Agent1", "Agent2"],
            enable_return_to_parent=True
        )

        runner = AgentRunner(registry)
        result = await runner.run_workflow(
            workflow_name="test_workflow",
            input_task="Workflow task",
            context=AgentContext(),
            max_turns=10
        )

        assert result.final_output is not None
        assert len(result.intermediate_results) > 0
        assert "Orchestrator" in result.agent_history
        assert "Agent1" in result.agent_history or "Agent2" in result.agent_history

    @pytest.mark.asyncio
    async def test_parallel_execution(self):
        """Test parallel execution of multiple agents."""
        registry = AgentRegistry()
        registry.register("Agent1", {"instructions": "Task 1"})
        registry.register("Agent2", {"instructions": "Task 2"})

        runner = AgentRunner(registry)
        agent_tasks = [
            ("Agent1", "Task 1"),
            ("Agent2", "Task 2")
        ]

        results = await runner.run_parallel(agent_tasks, context=AgentContext(), max_turns=5)

        assert len(results) == 2
        assert all(isinstance(r, RunResult) for r in results)
        assert all(r.final_output is not None for r in results)
        assert "Agent1 processed" in str(results[0].final_output)
        assert "Agent2 processed" in str(results[1].final_output)

    @pytest.mark.asyncio
    async def test_error_handling_nonexistent_agent(self):
        """Test error handling for nonexistent agent."""
        registry = AgentRegistry()
        runner = AgentRunner(registry)

        result = await runner.run(
            entry_agent_name="NonExistentAgent",
            input_task="Test task",
            context=AgentContext(),
            max_turns=5
        )

        assert result.final_output is None
        assert result.error is not None
        assert "not found" in result.error