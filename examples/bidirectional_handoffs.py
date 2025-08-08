"""
Bidirectional Handoff Example with Registry

This example demonstrates the new bidirectional handoff functionality that allows
sub-agents to return control to their parent agent, enabling orchestrator-like
workflows using a scalable registry-based approach.

This addresses Issue #1376: Bidirectional Handoff System
Author: Ayesha (github.com/CodeVoyager007)
Date: 2025-08-08
"""

import asyncio
from typing import Any
from agents import Agent, Runner
from agents.handoffs import create_bidirectional_handoff_workflow
from agents.registry import AgentRegistry, AgentRunner, RunResult


async def basic_bidirectional_example():
    """Basic example showing bidirectional handoffs with registry."""
    print("=== Basic Bidirectional Handoff Example ===\n")
    
    # Create registry and register agents
    registry = AgentRegistry()
    registry.register("Orchestrator", {
        "instructions": (
            "You are an orchestrator agent that coordinates workflows. "
            "You can hand off tasks to specialized agents and they will return control to you. "
            "When a sub-agent returns control, you can then hand off to other agents as needed. "
            "Always provide clear instructions to sub-agents about what you want them to do."
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

    # Get agents from registry
    orchestrator = registry.get_agent("Orchestrator")
    financial_agent = registry.get_agent("FinancialAgent")
    docs_agent = registry.get_agent("DocsAgent")

    # Set up bidirectional workflow
    orchestrator, sub_agents = create_bidirectional_handoff_workflow(
        orchestrator_agent=orchestrator,
        sub_agents=[financial_agent, docs_agent],
        enable_return_to_parent=True,
    )

    # Run the workflow using registry runner
    runner = AgentRunner(registry)
    result = await runner.run(
        entry_agent_name="Orchestrator",
        input_task=(
            "Fetch the quarterly financials for AAPL and save them in a Google Doc called "
            "'Apple Financials'. Make sure to analyze the key metrics and include your insights."
        ),
        max_turns=10,
    )

    print("Workflow completed!")
    print(f"Final output: {result.final_output}")
    print(f"Turn count: {result.turn_count}")
    print(f"Agent history: {result.agent_history}")
    print(f"Intermediate results: {len(result.intermediate_results)} items")


async def advanced_orchestrator_example():
    """Advanced example with multiple sub-agents and complex coordination."""
    print("\n=== Advanced Orchestrator Example ===\n")
    
    # Create registry and register agents
    registry = AgentRegistry()
    registry.register("WorkflowOrchestrator", {
        "instructions": (
            "You are a sophisticated workflow orchestrator. "
            "You coordinate complex multi-step processes by delegating to specialized agents. "
            "You can hand off to multiple agents and they will return control to you. "
            "After each agent returns, evaluate the results and decide on the next steps. "
            "You can hand off to different agents based on the current state and requirements."
        )
    })
    
    registry.register("ResearchAgent", {
        "instructions": (
            "You are a research specialist. "
            "When given a research task, conduct thorough research and gather relevant information. "
            "After completing your research, use return_to_parent to hand control back. "
            "Provide comprehensive findings and insights."
        )
    }, parent_name="WorkflowOrchestrator")
    
    registry.register("AnalysisAgent", {
        "instructions": (
            "You are a data analysis specialist. "
            "When given data to analyze, perform detailed analysis and provide insights. "
            "After completing your analysis, use return_to_parent to hand control back. "
            "Provide clear analysis results and recommendations."
        )
    }, parent_name="WorkflowOrchestrator")
    
    registry.register("ReportingAgent", {
        "instructions": (
            "You are a reporting specialist. "
            "When given information to report on, create comprehensive reports and summaries. "
            "After completing your report, use return_to_parent to hand control back. "
            "Provide well-structured reports with key findings."
        )
    }, parent_name="WorkflowOrchestrator")

    # Get agents from registry
    orchestrator = registry.get_agent("WorkflowOrchestrator")
    research_agent = registry.get_agent("ResearchAgent")
    analysis_agent = registry.get_agent("AnalysisAgent")
    reporting_agent = registry.get_agent("ReportingAgent")

    # Set up the bidirectional workflow
    orchestrator, sub_agents = create_bidirectional_handoff_workflow(
        orchestrator_agent=orchestrator,
        sub_agents=[research_agent, analysis_agent, reporting_agent],
        enable_return_to_parent=True,
    )

    # Run the complex workflow
    runner = AgentRunner(registry)
    result = await runner.run(
        entry_agent_name="WorkflowOrchestrator",
        input_task=(
            "Research the current state of renewable energy adoption in Europe, "
            "analyze the key trends and challenges, and create a comprehensive report "
            "with recommendations for future development."
        ),
        max_turns=15,
    )

    print("Advanced workflow completed!")
    print(f"Final output: {result.final_output}")
    print(f"Turn count: {result.turn_count}")
    print(f"Agent history: {result.agent_history}")
    print(f"Intermediate results: {len(result.intermediate_results)} items")


async def workflow_registry_example():
    """Example using pre-configured workflow registries."""
    print("\n=== Workflow Registry Example ===\n")
    
    # Use pre-configured financial workflow registry
    from agents.registry import create_financial_workflow_registry
    
    registry = create_financial_workflow_registry()
    runner = AgentRunner(registry)
    
    # Run the workflow
    result = await runner.run_workflow(
        workflow_name="financial_research",
        input_task="Fetch AAPL quarterly financials and save them in a Google Doc called 'Apple Financials'",
        max_turns=10,
    )
    
    print("Financial workflow completed!")
    print(f"Final output: {result.final_output}")
    print(f"Turn count: {result.turn_count}")
    print(f"Agent history: {result.agent_history}")


async def parallel_execution_example():
    """Example showing parallel execution of multiple agent tasks."""
    print("\n=== Parallel Execution Example ===\n")
    
    # Create registry with multiple agents
    registry = AgentRegistry()
    registry.register("TaskAgent1", {
        "instructions": "Complete task 1 and return results."
    })
    registry.register("TaskAgent2", {
        "instructions": "Complete task 2 and return results."
    })
    registry.register("TaskAgent3", {
        "instructions": "Complete task 3 and return results."
    })
    
    runner = AgentRunner(registry)
    
    # Define parallel tasks
    agent_tasks = [
        ("TaskAgent1", "Research market trends"),
        ("TaskAgent2", "Analyze competitor data"),
        ("TaskAgent3", "Generate summary report")
    ]
    
    # Run tasks in parallel
    results = await runner.run_parallel(
        agent_tasks=agent_tasks,
        max_turns=5
    )
    
    print("Parallel execution completed!")
    for i, result in enumerate(results):
        print(f"Task {i+1}: {result.final_output}")
        print(f"  Turn count: {result.turn_count}")
        print(f"  Error: {result.error}")


async def manual_setup_example():
    """Example showing manual setup of bidirectional handoffs."""
    print("\n=== Manual Setup Example ===\n")
    
    # Create registry and register agents manually
    registry = AgentRegistry()
    registry.register("ParentAgent", {
        "instructions": (
            "You are a parent agent that coordinates tasks. "
            "You can hand off to child agents and they will return control to you. "
            "After a child agent returns, you can hand off to other agents as needed."
        )
    })
    
    registry.register("ChildAgent1", {
        "instructions": (
            "You are the first child agent. "
            "Complete your assigned task and then use return_to_parent to hand control back."
        )
    }, parent_name="ParentAgent")
    
    registry.register("ChildAgent2", {
        "instructions": (
            "You are the second child agent. "
            "Complete your assigned task and then use return_to_parent to hand control back."
        )
    }, parent_name="ParentAgent")

    # Get agents from registry
    parent = registry.get_agent("ParentAgent")
    child1 = registry.get_agent("ChildAgent1")
    child2 = registry.get_agent("ChildAgent2")

    # Set up bidirectional workflow manually
    parent, sub_agents = create_bidirectional_handoff_workflow(
        orchestrator_agent=parent,
        sub_agents=[child1, child2],
        enable_return_to_parent=True,
    )

    # Run the workflow
    runner = AgentRunner(registry)
    result = await runner.run(
        entry_agent_name="ParentAgent",
        input_task="Task 1: Gather information. Task 2: Process the information.",
        max_turns=10,
    )

    print("Manual setup workflow completed!")
    print(f"Final output: {result.final_output}")
    print(f"Turn count: {result.turn_count}")
    print(f"Agent history: {result.agent_history}")


async def registry_management_example():
    """Example showing registry management features."""
    print("\n=== Registry Management Example ===\n")
    
    registry = AgentRegistry()
    
    # Register agents
    registry.register("Agent1", {"instructions": "First agent"})
    registry.register("Agent2", {"instructions": "Second agent"})
    registry.register("Agent3", {"instructions": "Third agent"})
    
    # Register workflow
    registry.register_workflow(
        workflow_name="test_workflow",
        orchestrator_name="Agent1",
        sub_agent_names=["Agent2", "Agent3"],
        enable_return_to_parent=True
    )
    
    # List registered items
    print(f"Registered agents: {registry.list_agents()}")
    print(f"Registered workflows: {registry.list_workflows()}")
    
    # Get configurations
    agent_config = registry.get_agent_config("Agent1")
    workflow_config = registry.get_workflow_config("test_workflow")
    
    print(f"Agent1 config: {agent_config}")
    print(f"Workflow config: {workflow_config}")
    
    # Remove items
    registry.remove_agent("Agent3")
    registry.remove_workflow("test_workflow")
    
    print(f"After removal - agents: {registry.list_agents()}")
    print(f"After removal - workflows: {registry.list_workflows()}")


async def main():
    """Run all bidirectional handoff examples."""
    print("Bidirectional Handoff Examples with Registry")
    print("=" * 60)
    print("This demonstrates the new bidirectional handoff functionality")
    print("using a scalable registry-based approach.\n")

    try:
        await basic_bidirectional_example()
        await advanced_orchestrator_example()
        await workflow_registry_example()
        await parallel_execution_example()
        await manual_setup_example()
        await registry_management_example()
        
        print("\n" + "=" * 60)
        print("All examples completed successfully!")
        print("\nKey benefits of registry-based bidirectional handoffs:")
        print("- Scalable agent management with weak references")
        print("- Pre-configured workflow templates")
        print("- Parallel execution capabilities")
        print("- Clean separation of concerns")
        print("- Easy workflow registration and management")
        print("- Orchestrator patterns with return-to-parent functionality")
        
    except Exception as e:
        print(f"Error running examples: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
