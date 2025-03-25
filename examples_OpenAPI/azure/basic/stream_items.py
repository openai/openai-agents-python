import asyncio
import random
import sys
import os

# Add project root to Python path to ensure src package can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from src.agents import Agent, ItemHelpers, Runner, function_tool
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.azure_openai_provider import AzureOpenAIProvider


@function_tool
def how_many_jokes() -> int:
    """Returns a random number between 1 and 10, representing the number of jokes to tell."""
    return random.randint(1, 10)


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
        name="Joker",
        instructions="First call the `how_many_jokes` tool, then tell that many jokes.",
        tools=[how_many_jokes],
        model_settings=azure_settings
    )
    
    # Run the streaming Agent
    result = Runner.run_streamed(
        agent,
        input="Tell me some jokes",
        run_config=run_config
    )
    
    print("=== Run starting ===")
    async for event in result.stream_events():
        # Ignore raw response event deltas
        if event.type == "raw_response_event":
            continue
        elif event.type == "agent_updated_stream_event":
            print(f"Agent updated: {event.new_agent.name}")
            continue
        elif event.type == "run_item_stream_event":
            if event.item.type == "tool_call_item":
                print("-- Tool was called")
            elif event.item.type == "tool_call_output_item":
                print(f"-- Tool output: {event.item.output}")
            elif event.item.type == "message_output_item":
                print(f"-- Message output:\n {ItemHelpers.text_message_output(event.item)}")
            else:
                pass  # Ignore other event types

    print("=== Run complete ===")


if __name__ == "__main__":
    # Print usage instructions
    print("Azure OpenAI Streaming Items Example")
    print("==================================")
    print("This example requires Azure OpenAI credentials.")
    print("Make sure you have set these environment variables:")
    print("- AZURE_OPENAI_API_KEY: Your Azure OpenAI API key")
    print("- AZURE_OPENAI_ENDPOINT: Your Azure OpenAI endpoint URL")
    print("- AZURE_OPENAI_API_VERSION: (Optional) API version")
    print("- AZURE_OPENAI_DEPLOYMENT: (Optional) Deployment name")
    print()
    
    # Run the main function
    asyncio.run(main())
