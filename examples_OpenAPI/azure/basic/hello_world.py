import asyncio
import sys
import os

# Add project root to Python path to ensure src package can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from src.agents import Agent, Runner
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.azure_openai_provider import AzureOpenAIProvider

"""
A simple "Hello World" example using Azure OpenAI service.
This demonstrates the most basic way to use the Agents SDK with Azure OpenAI.
"""

async def main():
    # Create run configuration
    run_config = RunConfig()
    
    # Create provider directly, it will automatically read configuration from environment variables
    run_config.model_provider = AzureOpenAIProvider()
    
    # Create Azure OpenAI model settings
    azure_settings = ModelSettings(
        provider="azure_openai",  # Specify Azure OpenAI as the provider
        temperature=0.7  # Optional: control creativity
    )
    
    agent = Agent(
        name="Hello World Agent",
        instructions="You are a friendly AI assistant that helps users.",
        model_settings=azure_settings,
    )
    
    # Run the agent
    result = await Runner.run(
        agent,
        input="Say hello to the world!",
        run_config=run_config,
    )
    
    # Print the result
    print(result.final_output)


if __name__ == "__main__":
    # Print usage instructions
    print("Azure OpenAI Hello World Example")
    print("==============================")
    print("This example requires Azure OpenAI credentials.")
    print("Make sure you have set these environment variables:")
    print("- AZURE_OPENAI_API_KEY: Your Azure OpenAI API key")
    print("- AZURE_OPENAI_ENDPOINT: Your Azure OpenAI endpoint URL")
    print("- AZURE_OPENAI_API_VERSION: (Optional) API version")
    print("- AZURE_OPENAI_DEPLOYMENT: (Optional) Deployment name")
    print()
    
    # Run the main function
    asyncio.run(main())
