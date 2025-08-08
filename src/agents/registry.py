"""
Agent Registry and Runner for Bidirectional Handoff Support

This module provides a registry-based system for managing agents with bidirectional
handoff capabilities, enabling scalable orchestrator-like workflows.

This addresses Issue #1376: Bidirectional Handoff System
Author: Ayesha (github.com/CodeVoyager007)
Date: 2025-08-08
"""

import asyncio
import weakref
import json
from typing import Any, Optional, Dict, List, Tuple, Union
from dataclasses import dataclass, field
from contextlib import asynccontextmanager

from agent import Agent
from .run import Runner
from .exceptions import AgentError
from .run_context import RunContextWrapper


@dataclass
class RunResult:
    """Result of an agent workflow execution."""
    final_output: Any
    intermediate_results: List[Any] = field(default_factory=list)
    error: Optional[str] = None
    turn_count: int = 0
    agent_history: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable representation of results."""
        return {
            'final_output': str(self.final_output) if self.final_output else None,
            'intermediate_results': [str(r) for r in self.intermediate_results],
            'error': self.error,
            'turn_count': self.turn_count,
            'agent_history': self.agent_history
        }


class AgentRegistry:
    """Central registry for agent configurations with weak reference management.
    
    This registry provides a scalable way to manage agents with bidirectional
    handoff capabilities, enabling orchestrator-like workflows.
    """
    
    def __init__(self):
        self.agent_configs: Dict[str, dict] = {}
        self.agent_instances: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
        self.parent_child_relationships: Dict[str, str] = {}
        self.workflow_configs: Dict[str, dict] = {}

    def register(
        self, 
        name: str, 
        config: dict,
        parent_name: Optional[str] = None,
        return_to_parent_enabled: bool = True
    ) -> None:
        """Register an agent configuration.
        
        Args:
            name: Name of the agent
            config: Agent configuration dictionary
            parent_name: Optional parent agent name for bidirectional handoffs
            return_to_parent_enabled: Whether this agent can return to parent
        """
        self.agent_configs[name] = config
        
        if parent_name:
            self.parent_child_relationships[name] = parent_name
            
        # Store return_to_parent setting
        if 'return_to_parent_enabled' not in config:
            config['return_to_parent_enabled'] = return_to_parent_enabled

    def get_agent(
        self, 
        name: str, 
        parent: Optional[Agent] = None,
        create_if_missing: bool = True
    ) -> Agent:
        """Retrieve or create an agent instance with parent reference.
        
        Args:
            name: Name of the agent to retrieve
            parent: Optional parent agent for bidirectional handoffs
            create_if_missing: Whether to create agent if not in registry
            
        Returns:
            Agent instance
            
        Raises:
            AgentError: If agent is not registered and create_if_missing is False
        """
        if name not in self.agent_configs:
            if not create_if_missing:
                raise AgentError(f"Agent '{name}' not registered")
            return None
        
        # Check if agent already exists in instances
        if name in self.agent_instances:
            agent = self.agent_instances[name]
            if parent:
                agent.set_parent(parent)
            return agent
        
        # Create new agent instance
        config = self.agent_configs[name]
        agent = Agent(
            name=name,
            instructions=config.get("instructions", ""),
            return_to_parent_enabled=config.get("return_to_parent_enabled", True)
        )
        
        # Set parent if provided
        if parent:
            agent.set_parent(parent)
        elif name in self.parent_child_relationships:
            # Set parent from registry relationship
            parent_name = self.parent_child_relationships[name]
            if parent_name in self.agent_instances:
                agent.set_parent(self.agent_instances[parent_name])
        
        # Store in instances
        self.agent_instances[name] = agent
        return agent

    def register_workflow(
        self,
        workflow_name: str,
        orchestrator_name: str,
        sub_agent_names: List[str],
        enable_return_to_parent: bool = True
    ) -> None:
        """Register a complete workflow configuration.
        
        Args:
            workflow_name: Name of the workflow
            orchestrator_name: Name of the orchestrator agent
            sub_agent_names: List of sub-agent names
            enable_return_to_parent: Whether to enable return-to-parent for sub-agents
        """
        self.workflow_configs[workflow_name] = {
            'orchestrator_name': orchestrator_name,
            'sub_agent_names': sub_agent_names,
            'enable_return_to_parent': enable_return_to_parent
        }

    def get_workflow_agents(
        self, 
        workflow_name: str
    ) -> Tuple[Agent, List[Agent]]:
        """Get all agents for a registered workflow.
        
        Args:
            workflow_name: Name of the workflow
            
        Returns:
            Tuple of (orchestrator_agent, sub_agents)
            
        Raises:
            AgentError: If workflow is not registered
        """
        if workflow_name not in self.workflow_configs:
            raise AgentError(f"Workflow '{workflow_name}' not registered")
        
        config = self.workflow_configs[workflow_name]
        orchestrator_name = config['orchestrator_name']
        sub_agent_names = config['sub_agent_names']
        
        # Get orchestrator
        orchestrator = self.get_agent(orchestrator_name)
        if not orchestrator:
            raise AgentError(f"Orchestrator agent '{orchestrator_name}' not found")
        
        # Get sub-agents
        sub_agents = []
        for sub_name in sub_agent_names:
            sub_agent = self.get_agent(sub_name, parent=orchestrator)
            if not sub_agent:
                raise AgentError(f"Sub-agent '{sub_name}' not found")
            sub_agents.append(sub_agent)
        
        return orchestrator, sub_agents

    def list_agents(self) -> List[str]:
        """List all registered agent names."""
        return list(self.agent_configs.keys())

    def list_workflows(self) -> List[str]:
        """List all registered workflow names."""
        return list(self.workflow_configs.keys())

    def get_agent_config(self, name: str) -> Optional[dict]:
        """Get agent configuration."""
        return self.agent_configs.get(name)

    def get_workflow_config(self, name: str) -> Optional[dict]:
        """Get workflow configuration."""
        return self.workflow_configs.get(name)

    def remove_agent(self, name: str) -> bool:
        """Remove an agent from the registry.
        
        Returns:
            True if agent was removed, False if not found
        """
        if name in self.agent_configs:
            del self.agent_configs[name]
            if name in self.agent_instances:
                del self.agent_instances[name]
            if name in self.parent_child_relationships:
                del self.parent_child_relationships[name]
            return True
        return False

    def remove_workflow(self, name: str) -> bool:
        """Remove a workflow from the registry.
        
        Returns:
            True if workflow was removed, False if not found
        """
        if name in self.workflow_configs:
            del self.workflow_configs[name]
            return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable representation of registry."""
        return {
            'registered_agents': list(self.agent_configs.keys()),
            'active_instances': list(self.agent_instances.keys()),
            'parent_child_relationships': self.parent_child_relationships,
            'workflow_configs': self.workflow_configs
        }

    def clear(self) -> None:
        """Clear all registered agents and workflows."""
        self.agent_configs.clear()
        self.agent_instances.clear()
        self.parent_child_relationships.clear()
        self.workflow_configs.clear()


class AgentRunner:
    """SDK-compatible agent workflow executor with registry support."""
    
    def __init__(self, registry: AgentRegistry):
        self.registry = registry
        self.max_turns: int = 10
        self.max_concurrent: int = 5

    async def run(
        self,
        entry_agent_name: str,
        input_task: str,
        context: Optional[Any] = None,
        max_turns: int = 10,
        max_concurrent: int = 5
    ) -> RunResult:
        """Execute agent workflow with dynamic agent selection.
        
        Args:
            entry_agent_name: Name of the entry agent
            input_task: Initial task to execute
            context: Optional context object
            max_turns: Maximum number of execution turns
            max_concurrent: Maximum concurrent tasks
            
        Returns:
            RunResult with final output and intermediate results
        """
        self.max_turns = max_turns
        self.max_concurrent = max_concurrent
        
        try:
            entry_agent = self.registry.get_agent(entry_agent_name)
            if not entry_agent:
                return RunResult(
                    final_output=None,
                    error=f"Entry agent '{entry_agent_name}' not found"
                )
            
            # Use the existing Runner.run() method
            from .run import Runner
            result = await Runner.run(
                starting_agent=entry_agent,
                input=input_task,
                context=context,
                max_turns=max_turns
            )
            
            return RunResult(
                final_output=result.final_output,
                intermediate_results=[str(item) for item in result.new_items],
                turn_count=result.turn_count,
                agent_history=[entry_agent_name]
            )
            
        except Exception as e:
            return RunResult(
                final_output=None,
                error=str(e)
            )

    async def run_workflow(
        self,
        workflow_name: str,
        input_task: str,
        context: Optional[Any] = None,
        max_turns: int = 10
    ) -> RunResult:
        """Execute a registered workflow.
        
        Args:
            workflow_name: Name of the registered workflow
            input_task: Initial task to execute
            context: Optional context object
            max_turns: Maximum number of execution turns
            
        Returns:
            RunResult with final output and intermediate results
        """
        try:
            orchestrator, sub_agents = self.registry.get_workflow_agents(workflow_name)
            
            # Set up bidirectional handoffs
            from .handoffs import create_bidirectional_handoff_workflow
            orchestrator, _ = create_bidirectional_handoff_workflow(
                orchestrator_agent=orchestrator,
                sub_agents=sub_agents,
                enable_return_to_parent=True
            )
            
            return await self.run(
                entry_agent_name=orchestrator.name,
                input_task=input_task,
                context=context,
                max_turns=max_turns
            )
            
        except Exception as e:
            return RunResult(
                final_output=None,
                error=str(e)
            )

    async def run_parallel(
        self,
        agent_tasks: List[Tuple[str, str]],
        context: Optional[Any] = None,
        max_turns: int = 10
    ) -> List[RunResult]:
        """Execute multiple agent tasks in parallel.
        
        Args:
            agent_tasks: List of (agent_name, task) tuples
            context: Optional context object
            max_turns: Maximum number of execution turns per task
            
        Returns:
            List of RunResult objects
        """
        tasks = [
            self.run(agent_name, task, context, max_turns)
            for agent_name, task in agent_tasks[:self.max_concurrent]
        ]
        return await asyncio.gather(*tasks, return_exceptions=True)

    @asynccontextmanager
    async def workflow_session(self, workflow_name: str):
        """Context manager for workflow sessions.
        
        Args:
            workflow_name: Name of the workflow to use
            
        Yields:
            AgentRunner configured for the workflow
        """
        try:
            # Set up workflow
            orchestrator, sub_agents = self.registry.get_workflow_agents(workflow_name)
            yield self
        finally:
            # Cleanup if needed
            pass


# Convenience functions for common patterns
def create_financial_workflow_registry() -> AgentRegistry:
    """Create a registry with a financial research workflow."""
    registry = AgentRegistry()
    
    # Register agents
    registry.register("Orchestrator", {
        "instructions": (
            "You are an orchestrator agent that coordinates financial research workflows. "
            "You can hand off tasks to specialized agents and they will return control to you. "
            "When a sub-agent returns, you can then hand off to other agents as needed."
        )
    })
    
    registry.register("FinancialAgent", {
        "instructions": (
            "You are a financial data specialist. "
            "When given a request for financial data, fetch and analyze the requested information. "
            "After completing your task, use the return_to_parent tool to hand control back to the orchestrator. "
            "Provide a summary of what you found."
        )
    }, parent_name="Orchestrator")
    
    registry.register("DocsAgent", {
        "instructions": (
            "You are a document management specialist. "
            "When given a request to save or create documents, handle the document operations. "
            "After completing your task, use the return_to_parent tool to hand control back to the orchestrator. "
            "Provide a summary of what you did."
        )
    }, parent_name="Orchestrator")
    
    # Register workflow
    registry.register_workflow(
        workflow_name="financial_research",
        orchestrator_name="Orchestrator",
        sub_agent_names=["FinancialAgent", "DocsAgent"],
        enable_return_to_parent=True
    )
    
    return registry


def create_support_workflow_registry() -> AgentRegistry:
    """Create a registry with a customer support workflow."""
    registry = AgentRegistry()
    
    # Register agents
    registry.register("TriageAgent", {
        "instructions": (
            "You are a customer support triage agent. "
            "Determine the type of customer issue and route appropriately to specialized agents. "
            "When agents return control, evaluate their results and decide on next steps."
        )
    })
    
    registry.register("BillingAgent", {
        "instructions": (
            "You are a billing specialist. "
            "Handle billing-related issues and return to triage when complete."
        )
    }, parent_name="TriageAgent")
    
    registry.register("TechnicalAgent", {
        "instructions": (
            "You are a technical support specialist. "
            "Handle technical issues and return to triage when complete."
        )
    }, parent_name="TriageAgent")
    
    # Register workflow
    registry.register_workflow(
        workflow_name="customer_support",
        orchestrator_name="TriageAgent",
        sub_agent_names=["BillingAgent", "TechnicalAgent"],
        enable_return_to_parent=True
    )
    
    return registry
