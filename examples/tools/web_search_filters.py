import asyncio
from datetime import datetime

from openai.types.responses.web_search_tool import Filters
from openai.types.shared.reasoning import Reasoning

from agents import Agent, ModelSettings, Runner, WebSearchTool, trace

# import logging
# logging.basicConfig(level=logging.DEBUG)


async def main():
    agent = Agent(
        name="WebOAI website searcher",
        model="gpt-5-nano",
        instructions="You are a helpful agent that can search openai.com resources.",
        tools=[
            WebSearchTool(
                # https://platform.openai.com/docs/guides/tools-web-search?api-mode=responses#domain-filtering
                filters=Filters(
                    allowed_domains=[
                        "openai.com",
                        "developer.openai.com",
                        "platform.openai.com",
                        "help.openai.com",
                    ],
                ),
                search_context_size="medium",
            )
        ],
        model_settings=ModelSettings(
            reasoning=Reasoning(effort="low"),
            verbosity="low",
            # https://platform.openai.com/docs/guides/tools-web-search?api-mode=responses#sources
            response_include=["web_search_call.action.sources"],
        ),
    )

    with trace("Web search example"):
        today = datetime.now().strftime("%Y-%m-%d")
        query = f"Write a summary of the latest OpenAI Platform updates for developers in the last few weeks (today is {today})."
        result = await Runner.run(agent, query)

        print()
        print("### Sources ###")
        print()
        for item in result.new_items:
            if item.type == "tool_call_item":
                if item.raw_item.type == "web_search_call":
                    for source in item.raw_item.action.sources:  # type: ignore [union-attr]
                        print(f"- {source.url}")
        print()
        print("### Final output ###")
        print()
        print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
