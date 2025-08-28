import asyncio

from pydantic import BaseModel

from agents import Agent, Runner, Usage, function_tool


class Weather(BaseModel):
    city: str
    temperature_range: str
    conditions: str


@function_tool
def get_weather(city: str) -> Weather:
    """Get the current weather information for a specified city."""
    return Weather(city=city, temperature_range="14-20C", conditions="Sunny with wind.")


def print_usage(usage: Usage) -> None:
    print("\n=== Usage ===")
    print(f"Requests: {usage.requests}")
    print(f"Input tokens: {usage.input_tokens}")
    print(f"Output tokens: {usage.output_tokens}")
    print(f"Total tokens: {usage.total_tokens}")


async def main() -> None:
    agent = Agent(
        name="Usage Demo",
        instructions="You are a concise assistant. Use tools if needed.",
        tools=[get_weather],
    )

    result = await Runner.run(agent, "What's the weather in Tokyo?")

    print("\nFinal output:")
    print(result.final_output)

    # Access usage from the run context
    print_usage(result.context_wrapper.usage)


if __name__ == "__main__":
    asyncio.run(main())
