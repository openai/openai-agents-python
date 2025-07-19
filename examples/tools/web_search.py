import asyncio

from agents import Agent, Runner, WebSearchTool, UserLocation, trace

async def main():
    agent = Agent(
        name="Web searcher",
        instructions="You are a helpful agent.",
        tools=[
            WebSearchTool(
                user_location=UserLocation(
                    type="approximate",
                    city="New York"
                ),
                # Feel free to adjust how much context the tool retrieves:
                # search_context_size="medium",
            )
        ],
    )

    with trace("Web search example"):
        result = await Runner.run(
            agent,
            "search the web for 'local sports news' and give me 1 interesting update in a sentence.",
        )
        print(result.final_output)
        # Now this _will_ be localized to New York!
    
if __name__ == "__main__":
    asyncio.run(main())
