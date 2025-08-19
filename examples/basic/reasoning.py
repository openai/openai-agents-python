import asyncio

from agents import Agent, Runner
from agents.model_settings import ModelSettings

URL = "https://www.berkshirehathaway.com/letters/2024ltr.pdf"


async def main():
    agent = Agent(
        name="Agent",
        model="o3",
        model_settings=ModelSettings(
            reasoning={
                "effort": "high",
                "summary": "auto",
            }
        ),
    )

    result = await Runner.run(agent, "How many r are in strawberry?")

    for item in result.new_items:
        if item.type == "reasoning_item":
            for key, value in item.raw_item:
                if key == "summary":
                    for summary in value:
                        print("Thought:", summary)

    print("Assistant:", result.final_output)

if __name__ == "__main__":
    asyncio.run(main())