"""Tests for the Exa search tool extension."""

from __future__ import annotations

import os
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from agents.extensions.exa_search import (
    ExaSearchResponse,
    ExaSearchResult,
    _extract_snippet,
    _format_results,
    _parse_results,
)

# ---------------------------------------------------------------------------
# Fixtures: mock Exa SDK response objects
# ---------------------------------------------------------------------------


@dataclass
class _MockResult:
    title: str = "Example Page"
    url: str = "https://example.com"
    published_date: str | None = "2024-01-15"
    author: str | None = "Author Name"
    score: float | None = 0.95
    text: str | None = "Full page text content here."
    highlights: list[str] | None = None
    summary: str | None = None


@dataclass
class _MockResponse:
    results: list[_MockResult]
    search_type: str | None = "neural"


def _mock_exa_client() -> tuple[MagicMock, MagicMock]:
    """Return (mock_exa_class, mock_client_instance) with headers dict."""
    mock_cls = MagicMock()
    mock_client = MagicMock()
    mock_client.headers = {}
    mock_cls.return_value = mock_client
    return mock_cls, mock_client


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


class TestParseResults:
    def test_parses_full_response(self) -> None:
        mock_resp = _MockResponse(
            results=[
                _MockResult(
                    title="Result 1",
                    url="https://example.com/1",
                    highlights=["key point"],
                    summary="A summary",
                ),
                _MockResult(title="Result 2", url="https://example.com/2"),
            ],
            search_type="neural",
        )
        parsed = _parse_results(mock_resp)
        assert len(parsed.results) == 2
        assert parsed.search_type == "neural"
        assert parsed.results[0].title == "Result 1"
        assert parsed.results[0].highlights == ["key point"]
        assert parsed.results[0].summary == "A summary"
        assert parsed.results[1].title == "Result 2"

    def test_parses_empty_response(self) -> None:
        mock_resp = _MockResponse(results=[])
        parsed = _parse_results(mock_resp)
        assert len(parsed.results) == 0
        assert parsed.search_type == "neural"

    def test_handles_missing_optional_fields(self) -> None:
        mock_resp = _MockResponse(
            results=[
                _MockResult(
                    title="Minimal",
                    url="https://example.com",
                    published_date=None,
                    author=None,
                    score=None,
                    text=None,
                    highlights=None,
                    summary=None,
                ),
            ],
        )
        parsed = _parse_results(mock_resp)
        result = parsed.results[0]
        assert result.title == "Minimal"
        assert result.published_date is None
        assert result.author is None
        assert result.text is None
        assert result.highlights is None
        assert result.summary is None


# ---------------------------------------------------------------------------
# Snippet extraction / content fallback
# ---------------------------------------------------------------------------


class TestExtractSnippet:
    def test_prefers_highlights(self) -> None:
        result = ExaSearchResult(
            title="T",
            url="https://example.com",
            highlights=["Point A", "Point B"],
            summary="A summary",
            text="Full text",
        )
        snippet = _extract_snippet(result)
        assert "Point A" in snippet
        assert "Point B" in snippet

    def test_falls_back_to_summary(self) -> None:
        result = ExaSearchResult(
            title="T",
            url="https://example.com",
            highlights=None,
            summary="A summary of the page",
            text="Full text",
        )
        assert _extract_snippet(result) == "A summary of the page"

    def test_falls_back_to_text(self) -> None:
        result = ExaSearchResult(
            title="T",
            url="https://example.com",
            highlights=None,
            summary=None,
            text="Some page text content",
        )
        assert _extract_snippet(result) == "Some page text content"

    def test_truncates_long_text(self) -> None:
        long_text = "x" * 1000
        result = ExaSearchResult(
            title="T",
            url="https://example.com",
            text=long_text,
        )
        snippet = _extract_snippet(result)
        assert len(snippet) == 503  # 500 chars + "..."
        assert snippet.endswith("...")

    def test_returns_empty_when_no_content(self) -> None:
        result = ExaSearchResult(title="T", url="https://example.com")
        assert _extract_snippet(result) == ""

    def test_empty_highlights_list_falls_through(self) -> None:
        result = ExaSearchResult(
            title="T",
            url="https://example.com",
            highlights=[],
            summary="Fallback summary",
        )
        assert _extract_snippet(result) == "Fallback summary"


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------


class TestFormatResults:
    def test_formats_multiple_results(self) -> None:
        parsed = ExaSearchResponse(
            results=[
                ExaSearchResult(
                    title="First Result",
                    url="https://example.com/1",
                    published_date="2024-01-01",
                    author="Alice",
                    highlights=["Important finding"],
                ),
                ExaSearchResult(
                    title="Second Result",
                    url="https://example.com/2",
                    text="Some text content",
                ),
            ],
        )
        formatted = _format_results(parsed)
        assert "1. First Result" in formatted
        assert "https://example.com/1" in formatted
        assert "Published: 2024-01-01" in formatted
        assert "Author: Alice" in formatted
        assert "Important finding" in formatted
        assert "2. Second Result" in formatted
        assert "https://example.com/2" in formatted

    def test_formats_empty_results(self) -> None:
        parsed = ExaSearchResponse(results=[])
        assert _format_results(parsed) == "No results found."


# ---------------------------------------------------------------------------
# Tool creation and configuration
# ---------------------------------------------------------------------------


class TestExaSearchToolCreation:
    def test_raises_import_error_when_exa_missing(self) -> None:
        with patch("agents.extensions.exa_search.Exa", None):
            from agents.extensions.exa_search import exa_search_tool

            with pytest.raises(ImportError, match="exa-py is required"):
                exa_search_tool(api_key="test-key")

    def test_raises_value_error_without_api_key(self) -> None:
        mock_cls, _mock_client = _mock_exa_client()
        env = {k: v for k, v in os.environ.items() if k != "EXA_API_KEY"}
        with (
            patch("agents.extensions.exa_search.Exa", mock_cls),
            patch.dict("os.environ", env, clear=True),
        ):
            from agents.extensions.exa_search import exa_search_tool

            with pytest.raises(ValueError, match="Exa API key is required"):
                exa_search_tool()

    def test_creates_function_tool_with_api_key(self) -> None:
        mock_cls, _mock_client = _mock_exa_client()
        with patch("agents.extensions.exa_search.Exa", mock_cls):
            from agents.extensions.exa_search import exa_search_tool

            tool = exa_search_tool(api_key="test-key-123")

            from agents.tool import FunctionTool

            assert isinstance(tool, FunctionTool)
            assert tool.name == "exa_search"
            assert "Exa" in tool.description

    def test_sets_integration_header(self) -> None:
        mock_cls, mock_client = _mock_exa_client()
        with patch("agents.extensions.exa_search.Exa", mock_cls):
            from agents.extensions.exa_search import exa_search_tool

            exa_search_tool(api_key="test-key-123")
            assert mock_client.headers["x-exa-integration"] == "openai-agents-python"

    def test_reads_api_key_from_env(self) -> None:
        mock_cls, _mock_client = _mock_exa_client()
        with (
            patch("agents.extensions.exa_search.Exa", mock_cls),
            patch.dict("os.environ", {"EXA_API_KEY": "env-key-456"}),
        ):
            from agents.extensions.exa_search import exa_search_tool

            exa_search_tool()
            mock_cls.assert_called_once_with("env-key-456")
