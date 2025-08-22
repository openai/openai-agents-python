"""
Basic workflow example demonstrating different connection types.

This example shows how to create a workflow that:
1. Uses handoff to route to a specialized agent
2. Uses tool connection to get analysis
3. Uses sequential connection to generate final output
"""

import asyncio

from pydantic import BaseModel

from agents import (
    Agent,
    HandoffConnection,
    SequentialConnection,
    ToolConnection,
    Workflow,
)


class WorkflowContext(BaseModel):
    """Shared context for the workflow."""

    user_language: str = "english"
    analysis_complete: bool = False


# Define specialized agents
triage_agent = Agent[WorkflowContext](
    name="Triage Agent",
    instructions=(
        "You are a triage agent that routes requests to appropriate specialists. "
        "Analyze the user's request and handoff to the content agent for processing."
    ),
    handoff_description="Routes requests to appropriate specialists",
)

content_agent = Agent[WorkflowContext](
    name="Content Agent",
    instructions=(
        "You are a content specialist. Process the user's request and prepare content. "
        "Use the analysis tool to get insights, then provide your response."
    ),
    handoff_description="Processes content requests",
)

analysis_agent = Agent[WorkflowContext](
    name="Analysis Agent",
    instructions=(
        "You are an analysis specialist. Provide detailed analysis and insights "
        "about the given content or request. Be thorough and analytical."
    ),
    handoff_description="Provides detailed analysis and insights",
)

summary_agent = Agent[WorkflowContext](
    name="Summary Agent",
    instructions=(
        "You are a summary specialist. Take the previous conversation and analysis "
        "and create a concise, well-structured final summary."
    ),
    handoff_description="Creates final summaries",
)


def extract_analysis_summary(result) -> str:
    """Extract just the key insights from analysis result."""
    # Custom output extractor for the analysis tool
    return f"Analysis insights: {result.final_output}"


async def main():
    """Run the basic workflow example."""
    print("Basic Workflow Example")
    print("======================\n")

    user_request = input("Enter your request: ")

    # Create the workflow with different connection types
    workflow = Workflow[WorkflowContext](
        connections=[
            # 1. Handoff from triage to content agent
            HandoffConnection(
                from_agent=triage_agent,
                to_agent=content_agent,
                tool_description_override="Transfer to content specialist for processing",
            ),
            # 2. Tool connection - content agent uses analysis agent as a tool
            ToolConnection(
                from_agent=content_agent,
                to_agent=analysis_agent,
                tool_name="get_analysis",
                tool_description="Get detailed analysis and insights",
                custom_output_extractor=extract_analysis_summary,
            ),
            # 3. Sequential connection - pass result to summary agent
            SequentialConnection(
                from_agent=content_agent,
                to_agent=summary_agent,
                output_transformer=lambda result: f"Previous conversation and analysis:\n{result.final_output}",
            ),
        ],
        name="Content Processing Workflow",
        context=WorkflowContext(user_language="english"),
        trace_workflow=True,
    )

    # Validate the workflow
    errors = workflow.validate_chain()
    if errors:
        print(f"Workflow validation errors: {errors}")
        return

    print("\nWorkflow info:")
    print(f"- {workflow.agent_count} unique agents")
    print(f"- {len(workflow.connections)} connections")
    print(f"- Start: {workflow.start_agent.name}")
    print(f"- End: {workflow.end_agent.name}")

    # Execute the workflow
    print("\nExecuting workflow...")

    try:
        result = await workflow.run(user_request)

        print(f"\n{'=' * 50}")
        print("WORKFLOW COMPLETED")
        print(f"{'=' * 50}")
        print(f"\nFinal Result:\n{result.final_result.final_output}")

        print("\nStep-by-step results:")
        for i, step_result in enumerate(result.step_results, 1):
            print(f"\nStep {i}: {step_result.last_agent.name}")
            print(f"Output: {str(step_result.final_output)[:200]}...")

    except Exception as e:
        print(f"Workflow failed: {e}")


async def demo_sync_execution():
    """Demonstrate synchronous workflow execution."""
    print("\n" + "=" * 50)
    print("SYNCHRONOUS EXECUTION DEMO")
    print("=" * 50)

    # Simple workflow for sync demo
    simple_workflow = Workflow[WorkflowContext](
        connections=[
            SequentialConnection(
                from_agent=triage_agent,
                to_agent=summary_agent,
            )
        ],
        name="Simple Sync Workflow",
        trace_workflow=False,  # Disable tracing for cleaner output
    )

    # Run synchronously
    result = simple_workflow.run_sync("Tell me about AI agents")
    print(f"Sync result: {result.final_result.final_output}")


if __name__ == "__main__":

    async def run_examples():
        await main()
        await demo_sync_execution()

    asyncio.run(run_examples())
