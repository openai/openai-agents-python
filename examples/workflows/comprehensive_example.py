"""
Comprehensive workflow example demonstrating all connection types.

This example showcases the full power of the Workflow system with:
- HandoffConnection: Routing and delegation
- ToolConnection: Modular agent functions
- SequentialConnection: Data transformation pipelines
- ConditionalConnection: Dynamic routing
- ParallelConnection: Concurrent processing
"""

import asyncio

from pydantic import BaseModel

from agents import (
    Agent,
    ConditionalConnection,
    HandoffConnection,
    ParallelConnection,
    SequentialConnection,
    ToolConnection,
    Workflow,
    function_tool,
    trace,
)


class ProjectContext(BaseModel):
    """Context for project management workflow."""

    project_type: str = "unknown"  # "research", "development", "creative"
    priority: str = "medium"  # "low", "medium", "high"
    requirements: list[str] = []
    stakeholders: list[str] = []


@function_tool
def add_requirement(requirement: str, context: ProjectContext) -> str:
    """Add a project requirement."""
    context.requirements.append(requirement)
    return f"Added requirement: {requirement}"


@function_tool
def add_stakeholder(stakeholder: str, context: ProjectContext) -> str:
    """Add a project stakeholder."""
    context.stakeholders.append(stakeholder)
    return f"Added stakeholder: {stakeholder}"


# Define specialized agents
intake_agent = Agent[ProjectContext](
    name="Intake Agent",
    instructions=(
        "You are a project intake specialist. Analyze incoming project requests "
        "and determine the project type (research/development/creative). "
        "Use the tools to record requirements and stakeholders. "
        "Then handoff to the appropriate specialist."
    ),
    tools=[add_requirement, add_stakeholder],
    handoff_description="Handles initial project intake and routing",
)

research_agent = Agent[ProjectContext](
    name="Research Specialist",
    instructions=(
        "You are a research project specialist. Handle research-focused projects "
        "with emphasis on methodology, data collection, and analysis planning."
    ),
    handoff_description="Handles research projects",
)

development_agent = Agent[ProjectContext](
    name="Development Specialist",
    instructions=(
        "You are a software development specialist. Handle development projects "
        "with focus on technical architecture, implementation, and deployment."
    ),
    handoff_description="Handles development projects",
)

creative_agent = Agent[ProjectContext](
    name="Creative Specialist",
    instructions=(
        "You are a creative project specialist. Handle creative projects "
        "with focus on design, content creation, and artistic direction."
    ),
    handoff_description="Handles creative projects",
)

risk_assessor = Agent[ProjectContext](
    name="Risk Assessor",
    instructions=(
        "You are a risk assessment specialist. Identify potential risks, "
        "challenges, and mitigation strategies for projects."
    ),
    handoff_description="Assesses project risks",
)

timeline_planner = Agent[ProjectContext](
    name="Timeline Planner",
    instructions=(
        "You are a timeline planning specialist. Create realistic project "
        "timelines, milestones, and delivery schedules."
    ),
    handoff_description="Creates project timelines",
)

coordinator_agent = Agent[ProjectContext](
    name="Project Coordinator",
    instructions=(
        "You are a project coordinator. Synthesize all project planning inputs "
        "and create a comprehensive project plan with clear next steps."
    ),
    handoff_description="Coordinates and synthesizes project planning",
)

quality_reviewer = Agent[ProjectContext](
    name="Quality Reviewer",
    instructions=(
        "You are a quality reviewer. Review project plans for completeness, "
        "feasibility, and quality. Provide feedback and recommendations."
    ),
    handoff_description="Reviews project plan quality",
)


# Condition functions for routing
def is_high_priority(context, previous_result) -> bool:
    """Check if project is high priority."""
    return bool(context.context.priority == "high")


def route_by_project_type(context, previous_result) -> bool:
    """Route based on project type - True for research, False for development."""
    return bool(context.context.project_type == "research")


async def main():
    """Demonstrate comprehensive workflow with all connection types."""
    print("Comprehensive Workflow Example")
    print("==============================\n")

    project_request = input("Describe your project: ")

    # Create context
    context = ProjectContext()

    print("\nChoose project priority:")
    print("1. Low")
    print("2. Medium")
    print("3. High")

    priority_choice = input("Select (1-3): ").strip()
    priority_map = {"1": "low", "2": "medium", "3": "high"}
    context.priority = priority_map.get(priority_choice, "medium")

    # Build dynamic workflow based on priority
    connections = [
        # 1. Always start with intake
        HandoffConnection(
            from_agent=intake_agent,
            to_agent=research_agent,  # Will be conditionally routed
        ),
        # 2. Conditional routing based on project type
        ConditionalConnection(
            from_agent=research_agent,
            to_agent=development_agent,  # Primary: development
            alternative_agent=creative_agent,  # Alternative: creative
            condition=route_by_project_type,
        ),
    ]

    # Add parallel processing for high-priority projects
    if context.priority == "high":
        connections.append(
            ParallelConnection(
                from_agent=development_agent,  # This will be the last agent from conditional
                to_agent=coordinator_agent,  # Not used in parallel connection
                parallel_agents=[risk_assessor, timeline_planner],
                synthesizer_agent=coordinator_agent,
                synthesis_template=(
                    "Project planning inputs:\n"
                    "Risk Assessment: {results}\n\n"
                    "Create a comprehensive project plan considering both perspectives."
                ),
            )
        )
    else:
        # For lower priority, use tool connection for lighter analysis
        connections.extend(
            [
                ToolConnection(
                    from_agent=development_agent,
                    to_agent=risk_assessor,
                    tool_name="assess_risks",
                    tool_description="Get risk assessment for the project",
                ),
                SequentialConnection(
                    from_agent=development_agent,
                    to_agent=coordinator_agent,
                    output_transformer=lambda result: f"Project details: {result.final_output}",
                ),
            ]
        )

    # Always end with quality review
    connections.append(
        SequentialConnection(
            from_agent=coordinator_agent,
            to_agent=quality_reviewer,
            output_transformer=lambda result: f"Project plan to review:\n{result.final_output}",
        )
    )

    # Create and validate workflow
    workflow = Workflow[ProjectContext](
        connections=connections,
        name=f"Project Planning Workflow ({context.priority} priority)",
        context=context,
        max_steps=10,
        trace_workflow=True,
    )

    # Validate workflow
    errors = workflow.validate_chain()
    if errors:
        print("Workflow validation errors:")
        for error in errors:
            print(f"  - {error}")
        return

    print("\nWorkflow Configuration:")
    print(f"- Priority: {context.priority}")
    print(f"- Agents: {workflow.agent_count}")
    print(f"- Connections: {len(workflow.connections)}")
    print(f"- Flow: {workflow.start_agent.name} → ... → {workflow.end_agent.name}")

    # Execute workflow
    print("\nExecuting workflow...")

    try:
        with trace("Comprehensive Project Planning"):
            result = await workflow.run(project_request)

        print(f"\n{'=' * 60}")
        print("WORKFLOW COMPLETED SUCCESSFULLY")
        print(f"{'=' * 60}")

        print(f"\nFinal Project Plan:\n{result.final_result.final_output}")

        print("\nWorkflow Execution Summary:")
        print(f"- Steps completed: {len(result.step_results)}")
        print(f"- Requirements gathered: {len(result.context.requirements)}")
        print(f"- Stakeholders identified: {len(result.context.stakeholders)}")
        print(f"- Final project type: {result.context.project_type}")

        if result.context.requirements:
            print("\nRequirements:")
            for req in result.context.requirements:
                print(f"  • {req}")

        if result.context.stakeholders:
            print("\nStakeholders:")
            for stakeholder in result.context.stakeholders:
                print(f"  • {stakeholder}")

        print("\nStep-by-step execution:")
        for i, step in enumerate(result.step_results, 1):
            agent_name = step.last_agent.name
            output_preview = (
                str(step.final_output)[:100] + "..."
                if len(str(step.final_output)) > 100
                else str(step.final_output)
            )
            print(f"  {i}. {agent_name}: {output_preview}")

    except Exception as e:
        print(f"Workflow execution failed: {e}")
        import traceback

        traceback.print_exc()


async def demo_workflow_cloning():
    """Demonstrate workflow cloning and modification."""
    print(f"\n{'=' * 60}")
    print("WORKFLOW CLONING DEMO")
    print(f"{'=' * 60}")

    # Create base workflow
    base_workflow = Workflow[ProjectContext](
        connections=[
            HandoffConnection(intake_agent, research_agent),
            SequentialConnection(research_agent, coordinator_agent),
        ],
        name="Base Workflow",
    )

    # Clone and modify
    enhanced_workflow = base_workflow.clone(
        name="Enhanced Workflow",
        max_steps=20,
    ).add_connection(SequentialConnection(coordinator_agent, quality_reviewer))

    print(f"Base workflow: {len(base_workflow.connections)} connections")
    print(f"Enhanced workflow: {len(enhanced_workflow.connections)} connections")
    print(f"Enhanced workflow name: {enhanced_workflow.name}")


if __name__ == "__main__":

    async def run_all_demos():
        await main()
        await demo_workflow_cloning()

    asyncio.run(run_all_demos())
