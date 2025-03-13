import os
import asyncio
from dotenv import load_dotenv

from agents import (
    Agent, 
    Runner, 
    GeminiProvider, 
    RunConfig, 
    function_tool
)

# Load environment variables from .env file
load_dotenv()

# Get the API key from environment variables
gemini_api_key = os.environ.get("GEMINI_API_KEY")

if not gemini_api_key:
    raise ValueError("GEMINI_API_KEY environment variable is not set")

# Create a Gemini provider with your API key
gemini_provider = GeminiProvider(api_key=gemini_api_key)

# Define a simple function tool
@function_tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    # In a real application, this would call a weather API
    return f"The weather in {city} is sunny and 75°F."

# Define an agent using Gemini
agent = Agent(
    name="Gemini Assistant",
    instructions="You are a helpful assistant powered by Google Gemini.",
    tools=[get_weather],
)

async def main():
    # Create a run configuration that uses the Gemini provider
    config = RunConfig(
        model_provider=gemini_provider,
        # Specify the model to use (default is "gemini-2.0-flash")
        model="gemini-2.0-flash",
    )
    
    # Run the agent with the Gemini provider
    result = await Runner.run(
        agent,
        "What's the weather like in Tokyo?",
        run_config=config,
    )
    
    # Print the final output
    print("\nFinal output:")
    print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())