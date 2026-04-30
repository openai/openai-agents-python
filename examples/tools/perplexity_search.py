from __future__ import annotations

import asyncio
import importlib.metadata
import os

import httpx

from agents import Agent, Runner, function_tool, set_tracing_disabled

"""Expose Perplexity's Search API as a function tool.

This shows how to wrap a direct HTTP call to `https://api.perplexity.ai/search` as a
`@function_tool` so any agent can use it. The Search API returns ranked web results
with titles, URLs, snippets, and dates — useful when you want raw search hits rather
than a model-summarised answer.

Set `PERPLEXITY_API_KEY` in your environment before running:

    export PERPLEXITY_API_KEY="..."
    uv run examples/tools/perplexity_search.py

Docs: https://docs.perplexity.ai/api-reference/search-post
"""

PERPLEXITY_SEARCH_URL = "https://api.perplexity.ai/search"
INTEGRATION_SLUG = "openai-agents"


def _attribution_header() -> str:
    try:
        version = importlib.metadata.version("openai-agents")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    return f"{INTEGRATION_SLUG}/{version}"


@function_tool
async def perplexity_search(query: str, max_results: int = 5) -> str:
    """Search the web with Perplexity's Search API.

    Args:
        query: The search query.
        max_results: Number of results to return (1-20).
    """
    api_key = os.environ.get("PERPLEXITY_API_KEY")
    if not api_key:
        return "Error: PERPLEXITY_API_KEY is not set."

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Pplx-Integration": _attribution_header(),
    }
    body = {"query": query, "max_results": max_results}

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(PERPLEXITY_SEARCH_URL, headers=headers, json=body)
        response.raise_for_status()
        data = response.json()

    results = data.get("results", [])
    if not results:
        return "No results found."

    formatted = []
    for i, item in enumerate(results, 1):
        title = item.get("title", "")
        url = item.get("url", "")
        snippet = item.get("snippet", "")
        formatted.append(f"{i}. {title}\n   {url}\n   {snippet}")
    return "\n\n".join(formatted)


async def main() -> None:
    set_tracing_disabled(disabled=True)
    agent = Agent(
        name="Researcher",
        instructions=(
            "You are a research assistant. Use the perplexity_search tool to find current "
            "information, then summarise the findings and cite the source URLs."
        ),
        tools=[perplexity_search],
    )

    result = await Runner.run(
        agent,
        "What were the headline announcements from the latest major AI developer conference?",
    )
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
