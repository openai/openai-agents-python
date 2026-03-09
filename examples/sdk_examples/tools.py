"""Custom tool creation and usage examples.

Demonstrates how to create function tools using decorators, use type annotations
for automatic schema generation, and build tools with advanced features like
context access and custom error handling.

Setup:
    export OPENAI_API_KEY="your-api-key"

Usage:
    python examples/sdk_examples/tools.py
"""

import asyncio
import json
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, Field

from agents import Agent, RunContextWrapper, Runner, function_tool
from agents.tool_context import ToolContext


# --- Example 1: Simple Function Tool ---


@function_tool
def add_numbers(a: int, b: int) -> int:
    """Add two numbers together and return the result."""
    return a + b


async def example_simple_tool() -> None:
    """Use a basic function tool with automatic schema generation."""
    agent = Agent(
        name="Calculator",
        instructions="You are a calculator. Use the add_numbers tool to add numbers.",
        tools=[add_numbers],
    )

    result = await Runner.run(agent, "What is 17 + 28?")
    print(f"[Simple Tool] {result.final_output}")


# --- Example 2: Tool with Annotated Parameters ---


class WeatherInfo(BaseModel):
    city: str = Field(description="The city name")
    temperature: str = Field(description="Temperature with unit")
    conditions: str = Field(description="Weather conditions")


@function_tool
def get_weather(
    city: Annotated[str, "The city to get weather for"],
    unit: Annotated[str, "Temperature unit: celsius or fahrenheit"] = "celsius",
) -> WeatherInfo:
    """Get the current weather for a city."""
    # Simulated weather data.
    weather_data: dict[str, dict[str, str]] = {
        "tokyo": {"temp_c": "18", "temp_f": "64", "conditions": "Partly cloudy"},
        "london": {"temp_c": "12", "temp_f": "54", "conditions": "Rainy"},
        "new york": {"temp_c": "22", "temp_f": "72", "conditions": "Sunny"},
    }
    data = weather_data.get(city.lower(), {"temp_c": "20", "temp_f": "68", "conditions": "Clear"})
    temp = data["temp_f"] if unit == "fahrenheit" else data["temp_c"]
    temp_unit = "F" if unit == "fahrenheit" else "C"
    return WeatherInfo(
        city=city,
        temperature=f"{temp} {temp_unit}",
        conditions=data["conditions"],
    )


async def example_annotated_tool() -> None:
    """Use a tool with Annotated type hints for parameter descriptions."""
    agent = Agent(
        name="Weather Bot",
        instructions="You are a weather assistant. Use the get_weather tool.",
        tools=[get_weather],
    )

    result = await Runner.run(agent, "What's the weather in Tokyo?")
    print(f"[Annotated Tool] {result.final_output}")


# --- Example 3: Tool with Context Access ---


@dataclass
class AppContext:
    """Application context tracking request count."""

    request_count: int = 0


@function_tool
def search_database(
    ctx: RunContextWrapper[AppContext],
    query: Annotated[str, "The search query"],
) -> str:
    """Search the database for information. Tracks request count in context."""
    ctx.context.request_count += 1
    # Simulated database results.
    results = {
        "python": "Python is a high-level programming language.",
        "rust": "Rust is a systems programming language focused on safety.",
    }
    return results.get(query.lower(), f"No results found for '{query}'.")


async def example_context_tool() -> None:
    """Use a tool that accesses the run context."""
    context = AppContext()
    agent = Agent[AppContext](
        name="Search Bot",
        instructions="Search the database when asked about programming languages.",
        tools=[search_database],
    )

    result = await Runner.run(agent, "Tell me about Python.", context=context)
    print(f"[Context Tool] {result.final_output}")
    print(f"[Context Tool] Requests made: {context.request_count}")


# --- Example 4: Tool with ToolContext ---


@function_tool
def log_and_process(
    ctx: ToolContext[None],
    value: Annotated[int, "A number to process"],
) -> str:
    """Process a value and log tool call metadata."""
    print(f"  [ToolContext] Tool name: {ctx.tool_name}")
    print(f"  [ToolContext] Call ID: {ctx.tool_call_id}")
    print(f"  [ToolContext] Arguments: {ctx.tool_arguments}")
    return json.dumps({"original": value, "doubled": value * 2, "squared": value**2})


async def example_tool_context() -> None:
    """Use ToolContext to access call metadata inside a tool."""
    agent = Agent(
        name="Processor",
        instructions="Process the number 7 using the log_and_process tool.",
        tools=[log_and_process],
    )

    result = await Runner.run(agent, "Process the number 7.")
    print(f"[ToolContext] {result.final_output}")


# --- Example 5: Multiple Tools Together ---


@function_tool
def convert_temperature(
    value: Annotated[float, "Temperature value"],
    from_unit: Annotated[str, "Source unit: celsius or fahrenheit"],
) -> str:
    """Convert temperature between Celsius and Fahrenheit."""
    if from_unit.lower() == "celsius":
        converted = (value * 9 / 5) + 32
        return f"{value}C = {converted:.1f}F"
    else:
        converted = (value - 32) * 5 / 9
        return f"{value}F = {converted:.1f}C"


@function_tool
def calculate_wind_chill(
    temp_f: Annotated[float, "Temperature in Fahrenheit"],
    wind_mph: Annotated[float, "Wind speed in mph"],
) -> str:
    """Calculate the wind chill factor given temperature and wind speed."""
    if temp_f > 50 or wind_mph < 3:
        return f"Wind chill not applicable (temp={temp_f}F, wind={wind_mph}mph)"
    wc = 35.74 + 0.6215 * temp_f - 35.75 * (wind_mph**0.16) + 0.4275 * temp_f * (wind_mph**0.16)
    return f"Wind chill: {wc:.1f}F (temp={temp_f}F, wind={wind_mph}mph)"


async def example_multiple_tools() -> None:
    """Give an agent multiple tools to choose from."""
    agent = Agent(
        name="Weather Calculator",
        instructions=(
            "You are a weather calculator. Use the available tools to answer "
            "questions about temperature conversion and wind chill."
        ),
        tools=[convert_temperature, calculate_wind_chill],
    )

    result = await Runner.run(agent, "Convert 30 degrees Celsius to Fahrenheit.")
    print(f"[Multiple Tools] {result.final_output}")


# --- Example 6: Async Function Tool ---


@function_tool
async def fetch_data(
    endpoint: Annotated[str, "The API endpoint name"],
) -> str:
    """Simulate an async API call to fetch data."""
    # Simulate async I/O delay.
    await asyncio.sleep(0.1)
    responses = {
        "users": '{"users": [{"name": "Alice"}, {"name": "Bob"}]}',
        "products": '{"products": [{"name": "Widget", "price": 9.99}]}',
    }
    return responses.get(endpoint, f'{{"error": "Unknown endpoint: {endpoint}"}}')


async def example_async_tool() -> None:
    """Use an async function tool that performs simulated I/O."""
    agent = Agent(
        name="API Agent",
        instructions="Fetch data from API endpoints when asked. Available endpoints: users, products",
        tools=[fetch_data],
    )

    result = await Runner.run(agent, "Fetch the list of users from the API.")
    print(f"[Async Tool] {result.final_output}")


# --- Run all examples ---


async def main() -> None:
    print("=" * 60)
    print("AGENTS SDK - Custom Tool Examples")
    print("=" * 60)

    examples = [
        ("1. Simple Function Tool", example_simple_tool),
        ("2. Annotated Parameters", example_annotated_tool),
        ("3. Tool with Context", example_context_tool),
        ("4. Tool with ToolContext", example_tool_context),
        ("5. Multiple Tools", example_multiple_tools),
        ("6. Async Function Tool", example_async_tool),
    ]

    for title, example_fn in examples:
        print(f"\n--- {title} ---")
        await example_fn()


if __name__ == "__main__":
    asyncio.run(main())
