import argparse
import asyncio
import os
import re
from urllib.parse import unquote, urlsplit, urlunsplit

from openai.types.responses.web_search_tool import Filters
from openai.types.shared.reasoning import Reasoning

from agents import Agent, ModelSettings, RunConfig, Runner, WebSearchTool, trace
from examples.web_search_utils import (
    extract_url_citations,
    extract_web_search_source_urls,
    get_field,
)

ALLOWED_DOMAINS = ["developers.openai.com"]
CODEX_DOCUMENTATION_URL = "https://developers.openai.com/api/docs/guides/agents-api/tools/functions"


# import logging
# logging.basicConfig(level=logging.DEBUG)


def _normalize_source_url(url: str) -> str | None:
    allowed_domains = {domain.lower().rstrip(".") for domain in ALLOWED_DOMAINS}
    blocked_suffixes = (
        ".css",
        ".eot",
        ".gif",
        ".ico",
        ".jpeg",
        ".jpg",
        ".js",
        ".png",
        ".svg",
        ".svgz",
        ".tar",
        ".tgz",
        ".woff",
        ".woff2",
        ".zip",
        ".gz",
    )

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None

    hostname = parsed.hostname.lower().rstrip(".") if parsed.hostname else None
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or not any(
            hostname == domain or hostname.endswith(f".{domain}") for domain in allowed_domains
        )
    ):
        return None

    path = parsed.path.rstrip("/")
    decoded_path = unquote(path)
    if (
        not path
        or any(character in decoded_path for character in "?#")
        or any(ord(character) < 32 or ord(character) == 127 for character in decoded_path)
        or decoded_path.lower().endswith(blocked_suffixes)
    ):
        return None

    return urlunsplit((parsed.scheme, hostname, path, "", ""))


def _normalized_source_urls(urls: list[str]) -> list[str]:
    normalized_urls: list[str] = []
    seen: set[str] = set()

    for url in urls:
        normalized = _normalize_source_url(url)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        normalized_urls.append(normalized)

    return normalized_urls


async def main(use_codex: bool = False):
    provider = None
    config = None
    if use_codex:
        from agents.models.codex import CodexProvider

        provider = CodexProvider()
        config = RunConfig(model_provider=provider)
    agent = Agent(
        name="WebOAI website searcher",
        model=os.environ.get("CODEX_MODEL") if use_codex else "gpt-5.6",
        instructions=(
            "You are a helpful agent that searches OpenAI developer documentation. Answer only "
            "from the allowed official documentation sources and include inline citations. Cite "
            "the official page for each model when comparing multiple models."
            + (
                " Individually open the canonical documentation page for each model named in the "
                "question before comparing them. A link in search results does not count as "
                "opening a page. Include inline Markdown links only to pages you opened; "
                "never cite an unopened page."
                if use_codex
                else ""
            )
        ),
        tools=[
            WebSearchTool(
                # https://platform.openai.com/docs/guides/tools-web-search?api-mode=responses#domain-filtering
                filters=Filters(allowed_domains=ALLOWED_DOMAINS),
                search_context_size="medium",
            )
        ],
        model_settings=ModelSettings(
            reasoning=Reasoning(effort="low"),
            tool_choice=None if use_codex else "required",
            verbosity="low",
            # https://platform.openai.com/docs/guides/tools-web-search?api-mode=responses#sources
            response_include=None if use_codex else ["web_search_call.action.sources"],
        ),
    )

    with trace("Web search example"):
        query = (
            "Using only official OpenAI developer documentation, compare GPT-5.6 Sol and "
            "GPT-5.6 Terra in three concise bullets and explain when to use each model."
        )
        if use_codex:
            query = (
                f"Open {CODEX_DOCUMENTATION_URL} with the web tool. Explain how function tools "
                "work in two concise bullets. Include an inline Markdown citation to that exact "
                "page, and cite no other page."
            )
        try:
            result = await Runner.run(agent, query, run_config=config)
        finally:
            if provider is not None:
                await provider.aclose()

        citations = extract_url_citations(result.new_items)
        cited_urls = _normalized_source_urls([citation.url for citation in citations])
        retrieved_urls = _normalized_source_urls(extract_web_search_source_urls(result.new_items))
        if use_codex:
            opened_urls: list[str] = []
            for item in result.new_items:
                raw = get_field(item, "raw_item")
                action = get_field(raw, "action")
                if (
                    get_field(raw, "type") == "web_search_call"
                    and get_field(raw, "status") == "completed"
                    and get_field(action, "type") == "open_page"
                ):
                    url = get_field(action, "url")
                    if not isinstance(url, str) or _normalize_source_url(url) is None:
                        raise RuntimeError("The web tool opened a disallowed source.")
                    opened_urls.append(url)
            links = re.findall(r"\[[^\]]*\]\((https?://[^)\s]+)\)", str(result.final_output))
            if any(_normalize_source_url(url) is None for url in links):
                raise RuntimeError("The final answer cited a disallowed source.")
            cited_urls = _normalized_source_urls(links)
            retrieved_urls = _normalized_source_urls(opened_urls)
            if not cited_urls or not set(cited_urls) <= set(retrieved_urls):
                raise RuntimeError(
                    f"Expected citations matching actual completed web open_page receipts. "
                    f"Opened: {retrieved_urls}; cited: {cited_urls}"
                )
            print("Verified opened sources:", retrieved_urls)
        model_documentation_urls = [
            url for url in retrieved_urls if "/api/docs/models/gpt-5.6-" in url
        ]

        if use_codex:
            if CODEX_DOCUMENTATION_URL not in retrieved_urls:
                raise RuntimeError(
                    "Expected the requested official page in completed open_page receipts"
                )
            model_documentation_urls = retrieved_urls
        if not cited_urls:
            raise RuntimeError("Expected at least one official inline citation in the final answer")
        if not model_documentation_urls:
            raise RuntimeError(
                f"Expected GPT-5.6 model documentation in retrieved sources, got {retrieved_urls}"
            )

        print()
        print("### Cited sources ###")
        print()
        for url in cited_urls:
            print(f"- {url}")
        print()
        print(
            "### Opened documentation ###" if use_codex else "### Retrieved model documentation ###"
        )
        print()
        for url in model_documentation_urls:
            print(f"- {url}")
        print()
        print("### Final output ###")
        print()
        print(result.final_output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Search official docs with verified sources.")
    parser.add_argument("--codex", action="store_true")
    asyncio.run(main(parser.parse_args().codex))
