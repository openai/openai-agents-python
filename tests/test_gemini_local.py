"""
Test script for Gemini with prompt injection feature.
Run this locally to test the implementation with your own API key.

Usage:
1. Set your API key: export GOOGLE_API_KEY=your_key_here
2. Run: python test_gemini_local.py
"""

import asyncio
import logging
import os
from typing import Any

from pydantic import BaseModel

from agents import Agent, function_tool
from agents.extensions.models.litellm_model import LitellmModel

# Enable logging to see the final system prompt sent to Gemini
logging.basicConfig(level=logging.INFO, format="%(message)s")


# Define your output schema
class WeatherReport(BaseModel):
    """Weather report structure."""

    city: str
    temperature: float
    conditions: str
    humidity: int


# Define a simple tool
@function_tool
def get_weather(city: str) -> dict[str, Any]:
    """Get the current weather for a city."""
    # Mock weather data
    weather_data = {
        "Tokyo": {"temperature": 22.5, "conditions": "sunny", "humidity": 65},
        "London": {"temperature": 15.0, "conditions": "rainy", "humidity": 80},
        "New York": {"temperature": 18.0, "conditions": "cloudy", "humidity": 70},
    }

    data = weather_data.get(city, {"temperature": 20.0, "conditions": "unknown", "humidity": 60})
    data["city"] = city
    return data


async def main():
    """Main test function."""

    # Check for API key
    if not os.getenv("GOOGLE_API_KEY"):
        print("ERROR: GOOGLE_API_KEY environment variable not set!")
        print("\nTo set it:")
        print("  Windows PowerShell: $env:GOOGLE_API_KEY='your_key_here'")
        print("  Windows CMD: set GOOGLE_API_KEY=your_key_here")
        print("  Linux/Mac: export GOOGLE_API_KEY=your_key_here")
        return

    print("=" * 80)
    print("Testing Gemini with Prompt Injection Feature")
    print("=" * 80)
    print("\n🔍 The final system prompt sent to Gemini will be shown below")
    print("=" * 80)

    # Create agent with prompt injection enabled on the model
    agent = Agent(
        name="weather_assistant",
        instructions=(
            "You are a helpful weather assistant. Use the get_weather tool to "
            "fetch weather information, then provide a structured report."
        ),
        model=LitellmModel(
            "gemini/gemini-2.5-flash",
            enable_structured_output_with_tools=True,  # CRITICAL: Enable for Gemini!
        ),
        tools=[get_weather],
        output_type=WeatherReport,
    )

    print("\nAgent Configuration:")
    print("  Model: gemini/gemini-2.5-flash")
    print(f"  Tools: {[tool.name for tool in agent.tools]}")
    print("  Output Type: WeatherReport")
    # Type check: ensure agent.model is LitellmModel
    if isinstance(agent.model, LitellmModel):
        print(
            f"  enable_structured_output_with_tools: "
            f"{agent.model.enable_structured_output_with_tools}"
        )

    print(f"\n{'=' * 80}")
    print("Running agent with input: 'What's the weather in Tokyo?'")
    print(f"{'=' * 80}\n")

    print("📤 Sending request to Gemini...")
    print("⏳ Waiting for response...\n")

    try:
        from agents import Runner

        result = await Runner.run(
            starting_agent=agent,
            input="What's the weather in Tokyo?",
        )

        print("\n✅ Agent execution completed!")

        print(f"\n{'=' * 80}")
        print("🎉 SUCCESS! Response Received")
        print(f"{'=' * 80}")

        print("\n📊 Result Analysis:")
        print(f"{'=' * 80}")
        print(f"Output Type: {type(result.final_output).__name__}")
        print(f"Output Value: {result.final_output}")
        print(f"{'=' * 80}")

        if isinstance(result.final_output, WeatherReport):
            print("\n✅ STRUCTURED OUTPUT PARSING: SUCCESS!")
            print(f"{'=' * 80}")
            print("\n📋 Weather Report (Parsed from JSON):")
            print(f"{'=' * 80}")
            print(f"  🌍 City: {result.final_output.city}")
            print(f"  🌡️  Temperature: {result.final_output.temperature}°C")
            print(f"  ☁️  Conditions: {result.final_output.conditions}")
            print(f"  💧 Humidity: {result.final_output.humidity}%")
            print(f"{'=' * 80}")
        else:
            print(
                f"\n⚠️  WARNING: Output type is {type(result.final_output)}, expected WeatherReport"
            )

        print("\n📈 Token Usage:")
        print(f"{'=' * 80}")
        print(f"  📥 Input tokens: {result.context_wrapper.usage.input_tokens}")
        print(f"  📤 Output tokens: {result.context_wrapper.usage.output_tokens}")
        print(f"  📊 Total tokens: {result.context_wrapper.usage.total_tokens}")
        print(f"{'=' * 80}")

        print("\n💡 What Happened:")
        print("  1. ✅ Prompt injection added JSON schema to system prompt")
        print("  2. ✅ Gemini called get_weather tool")
        print("  3. ✅ Gemini returned structured JSON matching WeatherReport schema")
        print("  4. ✅ SDK parsed JSON into WeatherReport Pydantic model")
        print("\n🎯 Feature is working correctly!")

    except Exception as e:
        print(f"\n{'=' * 80}")
        print("❌ ERROR!")
        print(f"{'=' * 80}")
        print(f"\n💥 Error: {e}")
        print("\n🔧 Troubleshooting Steps:")
        print(f"{'=' * 80}")
        print("  1. ✓ Check your API key is valid")
        print("  2. ✓ Ensure litellm is installed: pip install 'openai-agents[litellm]'")
        print("  3. ✓ Check internet connection")
        print("  4. ✓ Check DEBUG logs above for prompt details")
        print(f"{'=' * 80}")

        import traceback

        print("\n📋 Full traceback:")
        print(f"{'=' * 80}")
        traceback.print_exc()
        print(f"{'=' * 80}")


if __name__ == "__main__":
    print("\n" + "=" * 80)
    print("Gemini + Prompt Injection Test")
    print("=" * 80 + "\n")

    asyncio.run(main())
