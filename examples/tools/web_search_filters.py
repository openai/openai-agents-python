import asyncio
from urllib.parse import unquote, urlsplit, urlunsplit

from openai.types.responses.web_search_tool import Filters
from openai.types.shared.reasoning import Reasoning

from agents import Agent, ModelSettings, Runner, WebSearchTool, trace
from examples.web_search_utils import extract_url_citations, extract_web_search_source_urls

ALLOWED_DOMAINS = ["developers.openai.com"]


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


async def main():
    agent = Agent(
        name="WebOAI website searcher",
        model="gpt-5.6",
        instructions=(
            "You are a helpful agent that searches OpenAI developer documentation. Answer only "
            "from the allowed official documentation sources and include inline citations. Cite "
            "the official page for each model when comparing multiple models."
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
            tool_choice="required",
            verbosity="low",
            # https://platform.openai.com/docs/guides/tools-web-search?api-mode=responses#sources
            response_include=["web_search_call.action.sources"],
        ),
    )

    with trace("Web search example"):
        query = (
            "Using only official OpenAI developer documentation, compare GPT-5.6 Sol and "
            "GPT-5.6 Terra in three concise bullets and explain when to use each model."
        )
        result = await Runner.run(agent, query)

        citations = extract_url_citations(result.new_items)
        cited_urls = _normalized_source_urls([citation.url for citation in citations])
        retrieved_urls = _normalized_source_urls(extract_web_search_source_urls(result.new_items))
        model_documentation_urls = [
            url for url in retrieved_urls if "/api/docs/models/gpt-5.6-" in url
        ]

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
        print("### Retrieved model documentation ###")
        print()
        for url in model_documentation_urls:
            print(f"- {url}")
        print()
        print("### Final output ###")
        print()
        print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
