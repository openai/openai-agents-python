"""
Cross-Validated Web Search Example for OpenAI Agents

This example demonstrates how to use cross-validated-search with OpenAI Agents
for hallucination-free web search. Unlike standard web search, cross-validated-search
verifies facts across multiple sources and assigns confidence scores.

Installation:
    pip install cross-validated-search

Usage:
    python cross_validated_search.py
"""

import asyncio
from typing import Dict, Any, List
from dataclasses import dataclass

from agents import Agent, Runner, Tool, trace, function_tool


@dataclass
class CrossValidatedResult:
    """Result from cross-validated search."""
    answer: str
    confidence: str
    sources: List[Dict[str, str]]


def cross_validated_search(query: str) -> str:
    """
    Perform a cross-validated web search with confidence scoring.
    
    This tool searches the web and verifies facts across multiple sources
    to prevent hallucinations. Returns results with confidence levels.
    
    Args:
        query: The search query
    
    Returns:
        JSON string with answer, confidence, and sources
    """
    try:
        from cross_validated_search import CrossValidatedSearcher
    except ImportError:
        return '{"error": "cross-validated-search not installed. Run: pip install cross-validated-search"}'
    
    searcher = CrossValidatedSearcher()
    results = searcher.search(query)
    
    import json
    return json.dumps({
        "answer": results.answer,
        "confidence": results.confidence,
        "sources": [
            {
                "title": r.title,
                "url": r.url,
                "snippet": r.snippet[:100] + "..." if len(r.snippet) > 100 else r.snippet
            }
            for r in results.sources[:5]
        ]
    })


# Create the tool
cross_validated_web_search_tool = function_tool(
    name="cross_validated_search",
    description="Search the web with cross-validation for hallucination-free results. Verifies facts across multiple sources and returns confidence scores.",
)(cross_validated_search)


async def example_basic_search():
    """Example: Basic cross-validated search."""
    print("\n=== Basic Cross-Validated Search ===\n")
    
    agent = Agent(
        name="Cross-Validated Searcher",
        instructions="""You are a helpful assistant that searches the web with cross-validation.
        
When using search results:
1. Always report the confidence level (✅ Verified / 🟢 Likely True / 🟡 Uncertain)
2. Cite sources when presenting facts
3. If confidence is low, acknowledge uncertainty
4. Never present unverified information as fact""",
        tools=[cross_validated_web_search_tool],
    )
    
    with trace("Cross-validated search example"):
        result = await Runner.run(
            agent,
            "What is the latest version of Python?",
        )
        print(result.final_output)


async def example_fact_checking():
    """Example: Fact-checking with confidence filtering."""
    print("\n=== Fact-Checking Example ===\n")
    
    agent = Agent(
        name="Fact Checker",
        instructions="""You are a fact-checking assistant.
        
Use cross_validated_search to verify claims.
- Only accept verified facts (✅ Verified or 🟢 Likely True)
- Flag uncertain information with warnings
- Always cite your sources""",
        tools=[cross_validated_web_search_tool],
    )
    
    claims = [
        "Is Python 3.14 released?",
        "When was GPT-4 released?",
        "What is the population of Tokyo?",
    ]
    
    for claim in claims:
        with trace(f"Fact check: {claim}"):
            result = await Runner.run(
                agent,
                f"Fact-check this claim: {claim}",
            )
            print(f"\nClaim: {claim}")
            print(f"Result: {result.final_output}")


async def example_research_agent():
    """Example: Research agent with verified sources."""
    print("\n=== Research Agent Example ===\n")
    
    agent = Agent(
        name="Research Agent",
        instructions="""You are a research assistant that provides verified information.

When researching:
1. Use cross_validated_search for initial research
2. Filter results by confidence level
3. Only cite verified or likely true sources
4. Acknowledge when information is uncertain
5. Provide comprehensive answers with citations""",
        tools=[cross_validated_web_search_tool],
    )
    
    with trace("Research example"):
        result = await Runner.run(
            agent,
            "Research the latest advances in RAG (Retrieval-Augmented Generation) and summarize the key findings.",
        )
        print(result.final_output)


async def example_comparison_with_standard_search():
    """Example: Comparing cross-validated vs standard web search."""
    print("\n=== Comparison: Cross-Validated vs Standard Search ===\n")
    
    from agents import WebSearchTool
    
    # Agent with standard web search
    standard_agent = Agent(
        name="Standard Searcher",
        instructions="You use standard web search.",
        tools=[WebSearchTool()],
    )
    
    # Agent with cross-validated search
    cross_validated_agent = Agent(
        name="Cross-Validated Searcher",
        instructions="You use cross-validated web search with confidence scoring.",
        tools=[cross_validated_web_search_tool],
    )
    
    query = "What are the new features in Python 3.13?"
    
    print("Standard Web Search:")
    with trace("Standard search"):
        result = await Runner.run(standard_agent, query)
        print(result.final_output)
    
    print("\n" + "="*50 + "\n")
    
    print("Cross-Validated Web Search:")
    with trace("Cross-validated search"):
        result = await Runner.run(cross_validated_agent, query)
        print(result.final_output)


async def main():
    """Run all examples."""
    print("=" * 60)
    print("Cross-Validated Search Examples for OpenAI Agents")
    print("=" * 60)
    
    await example_basic_search()
    await example_fact_checking()
    await example_research_agent()
    
    # Uncomment to compare with standard search
    # await example_comparison_with_standard_search()
    
    print("\n" + "=" * 60)
    print("For more information:")
    print("https://github.com/wd041216-bit/cross-validated-search")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())