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

"""
This example demonstrates how to use function tools with Azure OpenAI service.
It creates a weather agent that can use tools to get weather information.
"""

# Create run configuration
run_config = RunConfig()

# Create provider directly, it will automatically read configuration from environment variables
run_config.model_provider = AzureOpenAIProvider()

# Create Azure OpenAI model settings
azure_settings = ModelSettings(
    provider="azure_openai",  # Specify Azure OpenAI as the provider
    temperature=0.7  # Optional: control creativity
)


# Define a data model for weather information
class Weather(BaseModel):
    city: str
    temperature_range: str
    conditions: str


# Create a function tool that returns weather information
@function_tool
def get_weather(city: str) -> Weather:
    """Get the current weather for a city"""
    print(f"[debug] Getting weather for {city}")
    
    # This is a mock implementation - in a real app, you might call a weather API
    weather_data = {
        "Tokyo": Weather(city="Tokyo", temperature_range="15-22C", conditions="Partly cloudy"),
        "New York": Weather(city="New York", temperature_range="10-18C", conditions="Rainy"),
        "London": Weather(city="London", temperature_range="8-15C", conditions="Foggy"),
        "Sydney": Weather(city="Sydney", temperature_range="20-28C", conditions="Sunny"),
    }
    
    # Return weather for the requested city, or a default response
    return weather_data.get(
        city, 
        Weather(city=city, temperature_range="15-25C", conditions="Weather data not available")
    )


# Create a temperature conversion tool
@function_tool
def convert_temperature(celsius: float) -> float:
    """Convert temperature from Celsius to Fahrenheit"""
    fahrenheit = (celsius * 9/5) + 32
    return round(fahrenheit, 1)


async def main():
    # Create the agent with tools
    weather_agent = Agent(
        name="Weather Assistant",
        instructions=(
            "You are a helpful weather assistant. When asked about weather, always use the get_weather tool. "
            "If users mention temperature in Celsius and want it in Fahrenheit, use the convert_temperature tool."
        ),
        tools=[get_weather, convert_temperature],
        model_settings=azure_settings,
    )
    
    # Get user input
    user_input = input("Ask me about the weather: ")
    
    # Run the agent
    print("\nProcessing your request...")
    result = await Runner.run(weather_agent, user_input, run_config=run_config)
    
    # Print the result
    print("\nResponse:")
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
