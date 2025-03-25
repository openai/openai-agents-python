import asyncio
import random
import sys
import os
from typing import Literal

# Add project root to Python path to ensure src package can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from src.agents import Agent, RunContextWrapper, Runner
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.azure_openai_provider import AzureOpenAIProvider


class CustomContext:
    def __init__(self, style: Literal["haiku", "pirate", "robot"]):
        self.style = style


def custom_instructions(
    run_context: RunContextWrapper[CustomContext], agent: Agent[CustomContext]
) -> str:
    context = run_context.context
    if context.style == "haiku":
        return "Only respond in haikus."
    elif context.style == "pirate":
        return "Respond as a pirate."
    else:
        return "Respond as a robot and say 'beep boop' a lot."


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
    
    # Create an Agent with dynamic instructions
    agent = Agent(
        name="Chat agent",
        instructions=custom_instructions,
        model_settings=azure_settings
    )

    # Randomly select a response style
    choice: Literal["haiku", "pirate", "robot"] = random.choice(["haiku", "pirate", "robot"])
    context = CustomContext(style=choice)
    print(f"Using style: {choice}\n")

    user_message = "Tell me a joke."
    print(f"User: {user_message}")
    result = await Runner.run(agent, user_message, context=context, run_config=run_config)

    print(f"Assistant: {result.final_output}")


if __name__ == "__main__":
    # Print usage instructions
    print("Azure OpenAI Dynamic System Prompt Example")
    print("=========================================")
    print("This example requires Azure OpenAI credentials.")
    print("Make sure you have set these environment variables:")
    print("- AZURE_OPENAI_API_KEY: Your Azure OpenAI API key")
    print("- AZURE_OPENAI_ENDPOINT: Your Azure OpenAI endpoint URL")
    print("- AZURE_OPENAI_API_VERSION: (Optional) API version")
    print("- AZURE_OPENAI_DEPLOYMENT: (Optional) Deployment name")
    print()
    
    # Run the main function
    asyncio.run(main())
