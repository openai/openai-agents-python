"""
Multi-Agent Research Pipeline Example

This example demonstrates a real-world research pipeline with three specialized agents:
1. Research Planner - breaks down complex queries into researchable topics
2. Deep Research Agent - investigates topics with web search capabilities
3. Summary Agent - synthesizes findings into actionable insights

This pattern is useful for: competitive analysis, technical research, market investigation, etc.
"""

import asyncio
from pydantic import BaseModel

from agents import Agent, Runner, trace


class ResearchPlan(BaseModel):
    topics: list[str]
    focus_areas: list[str]


class ResearchFindings(BaseModel):
    topic: str
    key_findings: list[str]
    sources: list[str]


class ResearchSummary(BaseModel):
    executive_summary: str
    key_takeaways: list[str]
    recommended_next_steps: list[str]


# Agent 1: Research Planner
planner_agent = Agent(
    name="research_planner",
    instructions="""You are a research strategist. Given a research query, break it down into 
    3-5 specific researchable topics and identify key focus areas. 
    Output a structured research plan with topics and focus areas.""",
    output_type=ResearchPlan,
)


# Agent 2: Deep Research Agent (simulated - in production, would have web search tools)
research_agent = Agent(
    name="deep_researcher",
    instructions="""You are a thorough research analyst. For the given topic, provide 
    3-5 key findings and potential sources. Be specific and factual.
    Format your response as a structured findings report.""",
    output_type=ResearchFindings,
)


# Agent 3: Summary Agent
summary_agent = Agent(
    name="research_summarizer",
    instructions="""You are a research synthesis expert. Given multiple research findings, 
    create an executive summary with key takeaways and recommended next steps.
    Be concise but comprehensive.""",
    output_type=ResearchSummary,
)


async def main():
    # Example research query
    query = input("Enter your research query: ") or "AI agents in healthcare 2026"

    with trace("Research Pipeline"):
        # Step 1: Create research plan
        print("\n[1/3] Creating research plan...")
        plan_result = await Runner.run(planner_agent, query)
        plan: ResearchPlan = plan_result.final_output
        print(f"  ✓ Identified {len(plan.topics)} research topics")

        # Step 2: Research each topic in parallel
        print("\n[2/3] Conducting deep research...")
        findings_results = await asyncio.gather(
            *[Runner.run(research_agent, f"Research this topic: {topic}") for topic in plan.topics]
        )
        findings: list[ResearchFindings] = [r.final_output for r in findings_results]
        print(f"  ✓ Completed {len(findings)} topic analyses")

        # Step 3: Synthesize into final summary
        print("\n[3/3] Synthesizing findings...")
        
        # Combine findings into a summary prompt
        findings_text = "\n\n".join(
            f"Topic: {f.topic}\nFindings: {'; '.join(f.key_findings)}"
            for f in findings
        )
        
        summary_result = await Runner.run(summary_agent, findings_text)
        summary: ResearchSummary = summary_result.final_output

        # Display results
        print("\n" + "=" * 60)
        print("RESEARCH SUMMARY")
        print("=" * 60)
        print(f"\n📊 Executive Summary:\n{summary.executive_summary}")
        print(f"\n💡 Key Takeaways:")
        for i, takeaway in enumerate(summary.key_takeaways, 1):
            print(f"  {i}. {takeaway}")
        print(f"\n➡️  Next Steps:")
        for i, step in enumerate(summary.recommended_next_steps, 1):
            print(f"  {i}. {step}")


if __name__ == "__main__":
    asyncio.run(main())