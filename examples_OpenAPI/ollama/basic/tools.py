import asyncio
import sys
import os

# Add project root to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from pydantic import BaseModel

from src.agents import Agent, Runner, function_tool
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

class Weather(BaseModel):
    city: str
    temperature_range: str
    conditions: str


@function_tool
def get_weather(city: str) -> Weather:
    print("[debug] get_weather called")
    return Weather(city=city, temperature_range="14-20C", conditions="Sunny with wind.")


async def main():
    # Create Ollama model settings
    ollama_settings = ModelSettings(
        provider="ollama",
        ollama_base_url="http://localhost:11434",
        ollama_default_model="PetrosStav/gemma3-tools:4b",
        temperature=0.7
    )
    # Create runtime configuration
    run_config = RunConfig(tracing_disabled=True)
    # Set model provider
    run_config.model_provider = ModelProviderFactory.create_provider(ollama_settings)
    
    agent = Agent(
        name="Hello world",
        instructions="You are a helpful agent.",
        tools=[get_weather],
        model_settings=ollama_settings
    )

    print("Running Agent with Ollama, please wait...")
    result = await Runner.run(
        agent, 
        input="What's the weather in Tokyo?",
        run_config=run_config
    )
    print("\nResult:")
    print(result.final_output)


if __name__ == "__main__":
    # Check if Ollama service is running
    import httpx
    try:
        response = httpx.get("http://localhost:11434/api/tags")
        if response.status_code != 200:
            print("Error: Ollama service returned non-200 status code. Please ensure Ollama service is running.")
            sys.exit(1)
    except Exception as e:
        print(f"Error: Cannot connect to Ollama service. Please ensure Ollama service is running.\n{str(e)}")
        print("\nIf you haven't installed Ollama, please download and install it from https://ollama.ai, then run 'ollama serve' to start the service")
        sys.exit(1)
        
    # Run the main function
    asyncio.run(main())
