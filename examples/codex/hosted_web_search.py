"""Run hosted Responses web search and print its citations and source URLs.

Run `python -m examples.codex.hosted_web_search` from the repository root
with the matching SDK and OPENAI_API_KEY. The account must support gpt-5.5
and hosted web search. Live requests can incur charges.
"""

import asyncio

from agents import Agent, WebSearch


async def main() -> None:
    agent = Agent(
        instructions="Search for the answer and cite your sources.",
        model="gpt-5.5",
        tools=[
            WebSearch(
                route="hosted",
                allowed_domains=["weather.gov"],
                blocked_domains=["forecast.weather.gov"],
                location={"country": "US", "city": "Chicago", "timezone": "America/Chicago"},
            )
        ],
        model_settings={"tool_choice": "required"},
    )
    async with agent.session() as session:
        turn = await session.submit("Where can I find official guidance about winter storms?")
        result = await turn.get_result()
        print(result.final_output)

        async for item in session.items():
            if item["type"] == "web_search_call":
                action = item["action"]
                if action is not None and action["type"] == "search":
                    for source in action.get("sources", []):
                        print("Consulted:", source["url"])
            elif item["type"] == "message" and item["role"] == "assistant":
                for part in item["content"]:
                    annotations = part.get("annotations")
                    if isinstance(annotations, list):
                        for annotation in annotations:
                            if (
                                isinstance(annotation, dict)
                                and annotation.get("type") == "url_citation"
                            ):
                                print("Cited:", annotation.get("title"), annotation.get("url"))


if __name__ == "__main__":
    asyncio.run(main())
