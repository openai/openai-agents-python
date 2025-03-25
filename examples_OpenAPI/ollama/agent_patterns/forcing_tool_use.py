import asyncio
import sys
import os
from typing import Any, Literal

# Add project root path to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from pydantic import BaseModel

from src.agents import (
    Agent,
    FunctionToolResult,
    ModelSettings,
    RunContextWrapper,
    Runner,
    ToolsToFinalOutputFunction,
    ToolsToFinalOutputResult,
    function_tool,
)
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

"""
This example demonstrates how to force an agent to use tools. It uses `ModelSettings(tool_choice="required")`
to force the agent to use any tool.

You can run it with 3 options:
1. `default`: Default behavior, sending tool output to the LLM. In this case, 
    `tool_choice` is not set, as it would otherwise cause an infinite loop - the LLM would call 
    the tool, the tool would run and send the result to the LLM, which would repeat (since the model 
    is forced to use a tool each time.)
2. `first_tool`: The first tool result is used as the final output.
3. `custom`: Use a custom tool use behavior function. The custom function receives all the tool results, 
    and chooses to generate the final output using the first tool result.

Usage:
python forcing_tool_use.py -t default
python forcing_tool_use.py -t first_tool
python forcing_tool_use.py -t custom
"""

def create_ollama_settings(model="phi3:latest"):
    """Create Ollama model settings"""
    return ModelSettings(
        provider="ollama",
        ollama_base_url="http://localhost:11434",
        ollama_default_model=model,
        temperature=0.7
    )

# Create run configuration
run_config = RunConfig(tracing_disabled=True)
# Set model provider
run_config.model_provider = ModelProviderFactory.create_provider(create_ollama_settings())


class Weather(BaseModel):
    city: str
    temperature_range: str
    conditions: str


@function_tool
def get_weather(city: str) -> Weather:
    print("[Debug] get_weather called")
    return Weather(city=city, temperature_range="14-20C", conditions="Sunny with wind")


async def custom_tool_use_behavior(
    context: RunContextWrapper[Any], results: list[FunctionToolResult]
) -> ToolsToFinalOutputResult:
    weather: Weather = results[0].output
    return ToolsToFinalOutputResult(
        is_final_output=True, final_output=f"The weather in {weather.city} is {weather.conditions}."
    )


async def main(tool_use_behavior: Literal["default", "first_tool", "custom"] = "default"):
    print(f"Running forcing tool use example with Ollama, mode: {tool_use_behavior}")
    
    if tool_use_behavior == "default":
        behavior: Literal["run_llm_again", "stop_on_first_tool"] | ToolsToFinalOutputFunction = (
            "run_llm_again"
        )
    elif tool_use_behavior == "first_tool":
        behavior = "stop_on_first_tool"
    elif tool_use_behavior == "custom":
        behavior = custom_tool_use_behavior

    # tool_choice is not needed in default mode as it would cause an infinite loop
    settings = create_ollama_settings()
    if tool_use_behavior != "default":
        settings.tool_choice = "required"
        
    agent = Agent(
        name="Weather Agent",
        instructions="You are a helpful agent.",
        tools=[get_weather],
        tool_use_behavior=behavior,
        model_settings=settings
    )

    result = await Runner.run(agent, input="What's the weather in Tokyo?", run_config=run_config)
    print(f"Result: {result.final_output}")


if __name__ == "__main__":
    # Check if Ollama service is running
    import httpx
    try:
        response = httpx.get("http://localhost:11434/api/tags")
        if response.status_code != 200:
            print("Error: Ollama service returned a non-200 status code. Make sure Ollama service is running.")
            sys.exit(1)
    except Exception as e:
        print(f"Error: Could not connect to Ollama service. Make sure Ollama service is running.\n{str(e)}")
        print("\nIf you haven't installed Ollama yet, download and install it from https://ollama.ai and start the service with 'ollama serve'")
        sys.exit(1)
        
    # Parse command line arguments
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-t",
        "--tool-use-behavior",
        type=str,
        default="default",
        choices=["default", "first_tool", "custom"],
        help="Tool use behavior. default sends tool output to the model. "
        "first_tool uses the first tool result as the final output. "
        "custom uses a custom tool use behavior function.",
    )
    args = parser.parse_args()
    
    # Run the main function
    asyncio.run(main(args.tool_use_behavior))
