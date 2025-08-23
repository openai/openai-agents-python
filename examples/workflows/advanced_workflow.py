"""
Advanced workflow example demonstrating complex orchestration patterns.

This example shows:
1. Conditional connections based on context
2. Parallel agent execution within workflows
3. Custom output transformers
4. Error handling and validation
"""

import asyncio
from typing import Any

from pydantic import BaseModel

from agents import (
    Agent,
    Runner,
    SequentialConnection,
    Workflow,
    WorkflowResult,
    function_tool,
    trace,
)


class ResearchContext(BaseModel):
    """Context for research workflow."""

    research_depth: str = "basic"  # "basic", "detailed", "comprehensive"
    target_audience: str = "general"
    findings: list[str] = []


@function_tool
def add_finding(finding: str, context: ResearchContext) -> str:
    """Add a research finding to the context."""
    context.findings.append(finding)
    return f"Added finding: {finding}"


# Research agents
researcher_agent = Agent[ResearchContext](
    name="Researcher",
    instructions=(
        "You are a research specialist. Conduct research on the given topic. "
        "Use the add_finding tool to record important discoveries. "
        "Based on the research depth in context, adjust your thoroughness."
    ),
    tools=[add_finding],
    handoff_description="Conducts research on topics",
)

fact_checker_agent = Agent[ResearchContext](
    name="Fact Checker",
    instructions=(
        "You are a fact-checking specialist. Verify the accuracy of research findings. "
        "Review the findings in the context and validate them."
    ),
    handoff_description="Verifies accuracy of research",
)

analyst_agent = Agent[ResearchContext](
    name="Analyst",
    instructions=(
        "You are a data analyst. Analyze research findings and extract insights. "
        "Look at the findings in the context and provide analytical insights."
    ),
    handoff_description="Analyzes research data",
)

writer_agent = Agent[ResearchContext](
    name="Writer",
    instructions=(
        "You are a content writer. Create well-structured content based on research and analysis. "
        "Adapt your writing style based on the target audience in the context."
    ),
    handoff_description="Creates final content",
)


def research_to_analysis_transformer(result: Any) -> str:
    """Transform research output for analysis."""
    return f"Research completed. Findings to analyze: {len(result.context.findings) if hasattr(result, 'context') else 'Unknown'}"


def analysis_to_writing_transformer(result: Any) -> str:
    """Transform analysis output for writing."""
    return f"Analysis phase complete. Ready for content creation based on: {str(result.final_output)[:100]}..."


class AdvancedWorkflowOrchestrator:
    """Orchestrates complex research workflows with different patterns."""

    def __init__(self):
        self.context = ResearchContext()

    async def run_research_workflow(
        self, topic: str, research_depth: str = "basic"
    ) -> WorkflowResult[ResearchContext]:
        """Run a research workflow with conditional depth."""

        # Update context
        self.context.research_depth = research_depth

        # Build workflow based on research depth
        connections = [
            # Always start with research
            SequentialConnection(
                from_agent=researcher_agent,
                to_agent=fact_checker_agent,
            )
        ]

        # Add analysis step for detailed research
        if research_depth in ["detailed", "comprehensive"]:
            connections.extend(
                [
                    SequentialConnection(
                        from_agent=fact_checker_agent,
                        to_agent=analyst_agent,
                    ),
                    SequentialConnection(
                        from_agent=analyst_agent,
                        to_agent=writer_agent,
                        output_transformer=analysis_to_writing_transformer,
                    ),
                ]
            )
        else:
            # Basic research goes straight to writing
            connections.append(
                SequentialConnection(
                    from_agent=fact_checker_agent,
                    to_agent=writer_agent,
                )
            )

        from typing import cast

        from agents.workflow.connections import Connection

        typed_connections = cast(list[Connection[ResearchContext]], connections)
        workflow = Workflow[ResearchContext](
            connections=typed_connections,
            name=f"Research Workflow ({research_depth})",
            context=self.context,
        )

        return await workflow.run(f"Research topic: {topic}")

    async def run_parallel_analysis_workflow(self, topic: str) -> WorkflowResult[ResearchContext]:
        """Run multiple analysis agents in parallel, then synthesize."""

        # Create multiple analysis agents for parallel processing
        technical_analyst = Agent[ResearchContext](
            name="Technical Analyst",
            instructions="Focus on technical aspects and implementation details.",
        )

        market_analyst = Agent[ResearchContext](
            name="Market Analyst",
            instructions="Focus on market trends and business implications.",
        )

        # Run parallel analysis using asyncio.gather
        with trace("Parallel Analysis Workflow"):
            # First, do initial research
            research_result = await Runner.run(
                researcher_agent,
                f"Research topic: {topic}",
                context=self.context,
            )

            # Then run parallel analysis
            technical_result, market_result = await asyncio.gather(
                Runner.run(
                    technical_analyst,
                    research_result.final_output,
                    context=self.context,
                ),
                Runner.run(
                    market_analyst,
                    research_result.final_output,
                    context=self.context,
                ),
            )

            # Finally, synthesize results
            synthesis_input = (
                f"Original research: {research_result.final_output}\n\n"
                f"Technical analysis: {technical_result.final_output}\n\n"
                f"Market analysis: {market_result.final_output}"
            )

            final_result = await Runner.run(
                writer_agent,
                synthesis_input,
                context=self.context,
            )

            return WorkflowResult(
                final_result=final_result,
                step_results=[research_result, technical_result, market_result, final_result],
                context=self.context,
            )


async def main():
    """Interactive demo of advanced workflows."""
    orchestrator = AdvancedWorkflowOrchestrator()

    print("Advanced Workflow Examples")
    print("==========================\n")

    topic = input("Enter a research topic: ")

    print("\nChoose workflow type:")
    print("1. Basic research")
    print("2. Detailed research (with analysis)")
    print("3. Comprehensive research (full pipeline)")
    print("4. Parallel analysis workflow")

    choice = input("\nSelect option (1-4): ").strip()

    try:
        if choice == "1":
            result = await orchestrator.run_research_workflow(topic, "basic")
        elif choice == "2":
            result = await orchestrator.run_research_workflow(topic, "detailed")
        elif choice == "3":
            result = await orchestrator.run_research_workflow(topic, "comprehensive")
        elif choice == "4":
            result = await orchestrator.run_parallel_analysis_workflow(topic)
        else:
            print("Invalid choice")
            return

        print(f"\n{'=' * 60}")
        print("WORKFLOW RESULTS")
        print(f"{'=' * 60}")
        print(f"\nFinal Output:\n{result.final_result.final_output}")

        print("\nWorkflow Statistics:")
        print(f"- Total steps executed: {len(result.step_results)}")
        print(f"- Final agent: {result.final_result.last_agent.name}")
        print(f"- Research findings collected: {len(result.context.findings)}")

        if result.context.findings:
            print("\nKey Findings:")
            for i, finding in enumerate(result.context.findings, 1):
                print(f"  {i}. {finding}")

    except Exception as e:
        print(f"Workflow execution failed: {e}")


if __name__ == "__main__":
    asyncio.run(main())
