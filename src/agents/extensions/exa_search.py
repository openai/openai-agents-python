"""Exa AI-powered search tool for OpenAI Agents.

Wraps the `Exa <https://exa.ai>`_ search API as a :class:`~agents.tool.FunctionTool`
so agents can perform neural web searches with content retrieval.

Usage::

    from agents import Agent
    from agents.extensions.exa_search import exa_search_tool

    agent = Agent(
        name="Research Agent",
        instructions="Use the Exa search tool to find information.",
        tools=[exa_search_tool()],
    )

Requires the ``exa`` optional dependency group::

    pip install "openai-agents[exa]"

The tool reads ``EXA_API_KEY`` from the environment by default.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

try:
    from exa_py import Exa
except ImportError:
    Exa = None  # type: ignore[assignment,misc]


@dataclass
class ExaSearchResult:
    """A single search result returned by the Exa API."""

    title: str
    url: str
    published_date: str | None = None
    author: str | None = None
    score: float | None = None
    text: str | None = None
    highlights: list[str] | None = None
    summary: str | None = None


@dataclass
class ExaSearchResponse:
    """Parsed response from the Exa search API."""

    results: list[ExaSearchResult] = field(default_factory=list)
    search_type: str | None = None


def _parse_results(response: Any) -> ExaSearchResponse:
    """Parse an Exa SDK response into typed dataclasses."""
    results: list[ExaSearchResult] = []
    for item in getattr(response, "results", []):
        results.append(
            ExaSearchResult(
                title=getattr(item, "title", "") or "",
                url=getattr(item, "url", "") or "",
                published_date=getattr(item, "published_date", None),
                author=getattr(item, "author", None),
                score=getattr(item, "score", None),
                text=getattr(item, "text", None),
                highlights=getattr(item, "highlights", None),
                summary=getattr(item, "summary", None),
            )
        )
    return ExaSearchResponse(
        results=results,
        search_type=getattr(response, "search_type", None),
    )


def _format_results(parsed: ExaSearchResponse) -> str:
    """Format parsed results into a readable string for the LLM."""
    if not parsed.results:
        return "No results found."

    parts: list[str] = []
    for i, result in enumerate(parsed.results, 1):
        lines = [f"{i}. {result.title}", f"   URL: {result.url}"]
        if result.published_date:
            lines.append(f"   Published: {result.published_date}")
        if result.author:
            lines.append(f"   Author: {result.author}")

        snippet = _extract_snippet(result)
        if snippet:
            lines.append(f"   {snippet}")

        parts.append("\n".join(lines))

    return "\n\n".join(parts)


def _extract_snippet(result: ExaSearchResult) -> str:
    """Build a content snippet, cascading through available content fields."""
    if result.highlights:
        return " ... ".join(result.highlights)
    if result.summary:
        return result.summary
    if result.text:
        text = result.text.strip()
        if len(text) > 500:
            return text[:500] + "..."
        return text
    return ""


def exa_search_tool(
    *,
    api_key: str | None = None,
    num_results: int = 5,
    text_max_characters: int = 1000,
    highlights: bool = True,
    summary: bool = False,
) -> Any:
    """Create an Exa search :class:`~agents.tool.FunctionTool`.

    Args:
        api_key: Exa API key. Falls back to the ``EXA_API_KEY`` environment variable.
        num_results: Default number of results to return per search.
        text_max_characters: Maximum characters of page text to retrieve per result.
        highlights: Whether to request highlight excerpts from results.
        summary: Whether to request a page summary for each result.

    Returns:
        A :class:`~agents.tool.FunctionTool` ready to be added to an agent's tool list.

    Raises:
        ImportError: If ``exa-py`` is not installed.
        ValueError: If no API key is provided and ``EXA_API_KEY`` is not set.
    """
    if Exa is None:
        raise ImportError(
            "exa-py is required for the Exa search tool. "
            "Install it with: pip install 'openai-agents[exa]'"
        )

    resolved_key = api_key or os.environ.get("EXA_API_KEY", "")
    if not resolved_key:
        raise ValueError(
            "An Exa API key is required. Pass api_key= or set the EXA_API_KEY environment variable."
        )

    client = Exa(resolved_key)
    client.headers["x-exa-integration"] = "openai-agents-python"

    # Build the default contents configuration.
    contents_kwargs: dict[str, Any] = {}
    if text_max_characters:
        contents_kwargs["text"] = {"max_characters": text_max_characters}
    if highlights:
        contents_kwargs["highlights"] = True
    if summary:
        contents_kwargs["summary"] = True

    default_num_results = num_results

    # Import here to avoid circular imports at module level.
    from ..tool import function_tool as _function_tool

    @_function_tool(
        name_override="exa_search",
        description_override=(
            "Search the web using Exa, an AI-powered search engine. "
            "Returns relevant web pages with titles, URLs, and content snippets. "
            "Supports neural search, domain filtering, date filtering, and category filtering."
        ),
    )
    def exa_search(
        query: str,
        num_results: int | None = None,
        search_type: str | None = None,
        category: str | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        include_text: list[str] | None = None,
        exclude_text: list[str] | None = None,
        start_published_date: str | None = None,
        end_published_date: str | None = None,
    ) -> str:
        """Search the web using Exa.

        Args:
            query: The search query.
            num_results: Number of results to return (1-100).
            search_type: Search method: 'auto', 'neural', or 'fast'.
            category: Focus area: 'company', 'research paper', 'news', 'personal site',
                'financial report', or 'people'.
            include_domains: Only include results from these domains.
            exclude_domains: Exclude results from these domains.
            include_text: Only include results containing these strings.
            exclude_text: Exclude results containing these strings.
            start_published_date: Filter results published after this date (ISO 8601).
            end_published_date: Filter results published before this date (ISO 8601).
        """
        kwargs: dict[str, Any] = {
            "query": query,
            "num_results": num_results or default_num_results,
            **contents_kwargs,
        }

        if search_type:
            kwargs["type"] = search_type
        if category:
            kwargs["category"] = category
        if include_domains:
            kwargs["include_domains"] = include_domains
        if exclude_domains:
            kwargs["exclude_domains"] = exclude_domains
        if include_text:
            kwargs["include_text"] = include_text
        if exclude_text:
            kwargs["exclude_text"] = exclude_text
        if start_published_date:
            kwargs["start_published_date"] = start_published_date
        if end_published_date:
            kwargs["end_published_date"] = end_published_date

        response = client.search_and_contents(**kwargs)
        parsed = _parse_results(response)
        return _format_results(parsed)

    return exa_search
