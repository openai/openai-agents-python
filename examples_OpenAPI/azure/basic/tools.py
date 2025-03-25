import asyncio
import sys
import os

# Add project root to Python path to ensure src package can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from pydantic import BaseModel
from src.agents import Agent, Runner, function_tool
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.azure_openai_provider import AzureOpenAIProvider


class Weather(BaseModel):
    city: str
    temperature_range: str
    conditions: str


@function_tool
def get_weather(city: str) -> Weather:
    print("[debug] get_weather called")
    return Weather(city=city, temperature_range="14-20C", conditions="Sunny with light clouds.")


async def main():
    # Create runtime configuration
    run_config = RunConfig()
    
    # Automatically create the provider, it will read configurations from environment variables
    run_config.model_provider = AzureOpenAIProvider()
    
    # Create Azure OpenAI model settings
    azure_settings = ModelSettings(
        provider="azure_openai",  # Specify Azure OpenAI as the provider
        temperature=0.7  # Optional: Control creativity
    )

    # Create an Agent instance
    agent = Agent(
        name="Weather Assistant",
        instructions="You are a helpful weather assistant. Use the get_weather tool when asked about weather.",
        model_settings=azure_settings,
        tools=[get_weather],
    )
    
    # Run the Agent
    print("Running Agent with Azure OpenAI, please wait...")
    result = await Runner.run(
        agent, 
        "What's the weather in Tokyo?", 
        run_config=run_config
    )
    
    # Print the result
    print("\nResult:")
    print(result.final_output)


if __name__ == "__main__":
    # Print usage instructions
    print("Azure OpenAI Tools Example")
    print("=========================")
    print("This example requires Azure OpenAI credentials.")
    print("Make sure you have set these environment variables:")
    print("- AZURE_OPENAI_API_KEY: Your Azure OpenAI API key")
    print("- AZURE_OPENAI_ENDPOINT: Your Azure OpenAI endpoint URL")
    print("- AZURE_OPENAI_API_VERSION: (Optional) API version")
    print("- AZURE_OPENAI_DEPLOYMENT: (Optional) Deployment name")
    print()
    
    # Run the main function
    asyncio.run(main())
