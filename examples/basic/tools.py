import asyncio
from typing import Annotated

from pydantic import BaseModel, Field

from agents import Agent, Runner, function_tool


class Weather(BaseModel):
    city: str = Field(description="The city name")
    temperature_range: str = Field(description="The temperature range in Celsius")
    conditions: str = Field(description="The weather conditions")


@function_tool
def get_weather(city: Annotated[str, "The city to get the weather for"]) -> Weather:
    """Get the current weather information for a specified city."""
    print("[debug] get_weather called")
    return Weather(city=city, temperature_range="14-20C", conditions="Sunny with wind.")


agent = Agent(
    name="Hello world",
    instructions="You are a helpful agent.",
    tools=[get_weather],
)


async def main():
    result = await Runner.run(agent, input="What's the weather in Tokyo?")
    print(result.final_output)
    # The weather in Tokyo is sunny.


if __name__ == "__main__":
    asyncio.run(main())
