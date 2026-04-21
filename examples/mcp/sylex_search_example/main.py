"""
Sylex Search MCP Example

Demonstrates how to use Sylex Search — a curated catalog of 11,000+ AI tools
and products — as an MCP tool source inside an OpenAI Agents SDK agent.

The agent searches the catalog to answer questions about AI products without
making any web requests or LLM-powered search calls. All results come from the
deterministic Sylex FTS index over SSE.

Run:
    uv run python examples/mcp/sylex_search_example/main.py
"""

import asyncio

from agents import Agent, Runner, gen_trace_id, trace
from agents.mcp import MCPServerSse

# Public Sylex Search SSE endpoint — no auth required.
SYLEX_SSE_URL = "https://mcp-server-production-38c9.up.railway.app/sse"


async def run_search_queries(agent: Agent) -> None:
    queries = [
        "What are some good vector database options for a Python project?",
        "Find me open-source alternatives to Pinecone for storing embeddings.",
        "Compare LangChain and LlamaIndex for building RAG pipelines.",
    ]

    for query in queries:
        print(f"\n{'=' * 60}")
        print(f"Query: {query}")
        print("=" * 60)
        result = await Runner.run(agent, query)
        print(result.final_output)


async def main() -> None:
    async with MCPServerSse(
        name="Sylex Search",
        params={"url": SYLEX_SSE_URL},
    ) as server:
        agent = Agent(
            name="Product Research Agent",
            instructions=(
                "You are a helpful AI product research assistant. "
                "Use the Sylex Search MCP tools to look up AI tools, libraries, "
                "and products from the catalog. "
                "When answering questions:\n"
                "1. Use search.discover to find relevant products by keyword.\n"
                "2. Use search.details to fetch full details for promising results.\n"
                "3. Use search.compare to contrast multiple options side by side.\n"
                "4. Use search.alternatives to find similar tools to a known product.\n"
                "Always cite the product names and brief descriptions from the catalog "
                "in your final answer."
            ),
            mcp_servers=[server],
        )

        trace_id = gen_trace_id()
        with trace(workflow_name="Sylex Search MCP Example", trace_id=trace_id):
            print(f"View trace: https://platform.openai.com/traces/trace?trace_id={trace_id}\n")
            await run_search_queries(agent)


if __name__ == "__main__":
    asyncio.run(main())
