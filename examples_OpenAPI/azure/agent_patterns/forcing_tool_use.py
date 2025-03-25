from __future__ import annotations

import asyncio
import sys
import os
from typing import Any, Literal

# Add project root to Python path to ensure src package can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from pydantic import BaseModel

from src.agents import (
    Agent,
    FunctionToolResult,
    RunContextWrapper,
    Runner,
    ToolsToFinalOutputFunction,
    ToolsToFinalOutputResult,
    function_tool,
)
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.azure_openai_provider import AzureOpenAIProvider

"""
This example demonstrates how to force the agent to use a tool with Azure OpenAI service.
It uses `ModelSettings(tool_choice="required")` to force the agent to use any tool.

You can run it with 3 options:
1. `default`: The default behavior, which is to send the tool output to the LLM. In this case,
    `tool_choice` is not set, because otherwise it would result in an infinite loop - the LLM would
    call the tool, the tool would run and send the results to the LLM, and that would repeat
    (because the model is forced to use a tool every time.)
2. `first_tool_result`: The first tool result is used as the final output.
3. `custom`: A custom tool use behavior function is used. The custom function receives all the tool
    results, and chooses to use the first tool result to generate the final output.

Usage:
python examples_OpenAPI/azure/agent_patterns/forcing_tool_use.py -t default
python examples_OpenAPI/azure/agent_patterns/forcing_tool_use.py -t first_tool
python examples_OpenAPI/azure/agent_patterns/forcing_tool_use.py -t custom
"""

# Create run configuration
run_config = RunConfig()

# Create provider directly, it will automatically read configuration from environment variables
run_config.model_provider = AzureOpenAIProvider()

# Create Azure OpenAI model settings (will be used in main function)
azure_settings = ModelSettings(
    provider="azure_openai",  # Specify Azure OpenAI as the provider
    temperature=0.7  # Optional: control creativity
)


class Weather(BaseModel):
    city: str
    temperature_range: str
    conditions: str


@function_tool
def get_weather(city: str) -> Weather:
    print("[debug] get_weather called")
    return Weather(city=city, temperature_range="14-20C", conditions="Sunny with wind")


async def custom_tool_use_behavior(
    context: RunContextWrapper[Any], results: list[FunctionToolResult]
) -> ToolsToFinalOutputResult:
    weather: Weather = results[0].output
    return ToolsToFinalOutputResult(
        is_final_output=True, final_output=f"{weather.city} is {weather.conditions}."
    )


async def main(tool_use_behavior: Literal["default", "first_tool", "custom"] = "default"):
    if tool_use_behavior == "default":
        behavior: Literal["run_llm_again", "stop_on_first_tool"] | ToolsToFinalOutputFunction = (
            "run_llm_again"
        )
    elif tool_use_behavior == "first_tool":
        behavior = "stop_on_first_tool"
    elif tool_use_behavior == "custom":
        behavior = custom_tool_use_behavior

    # Create agent with Azure settings
    agent = Agent(
        name="Weather agent",
        instructions="You are a helpful agent.",
        tools=[get_weather],
        tool_use_behavior=behavior,
        model_settings=ModelSettings(
            provider="azure_openai",
            tool_choice="required" if tool_use_behavior != "default" else None,
            temperature=0.7
        ),
    )

    result = await Runner.run(agent, input="What's the weather in Tokyo?", run_config=run_config)
    print(result.final_output)


if __name__ == "__main__":
    import argparse

    # Print usage instructions
    print("Azure OpenAI Forcing Tool Use Example")
    print("====================================")
    print("This example requires Azure OpenAI credentials.")
    print("Make sure you have set these environment variables:")
    print("- AZURE_OPENAI_API_KEY: Your Azure OpenAI API key")
    print("- AZURE_OPENAI_ENDPOINT: Your Azure OpenAI endpoint URL")
    print("- AZURE_OPENAI_API_VERSION: (Optional) API version")
    print("- AZURE_OPENAI_DEPLOYMENT: (Optional) Deployment name")
    print()

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-t",
        "--tool-use-behavior",
        type=str,
        required=True,
        choices=["default", "first_tool", "custom"],
        help="The behavior to use for tool use. Default will cause tool outputs to be sent to the model. "
        "first_tool_result will cause the first tool result to be used as the final output. "
        "custom will use a custom tool use behavior function.",
    )
    args = parser.parse_args()
    asyncio.run(main(args.tool_use_behavior))
