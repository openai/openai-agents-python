import asyncio
import os
from openai import AsyncOpenAI

from agents import Agent, Runner, function_tool, set_tracing_disabled
from agents.models.openai_responses import OpenAIResponsesModel

"""
This example demonstrates how to create an agent that hands off using groq's 'qwen-2.5-32b' model.
Add groq's API to variable - API_KEY.
"""

# Set custom parameters directly
BASE_URL = os.getenv("OPEN_RESPONSES_URL") or "http://localhost:8080/v1" #Either set OPEN_RESPONSES_URL in environment variable or put it directly here.
API_KEY = os.getenv("GROK_API_KEY") or "" #Either set GROK_API_KEY in environment variable or put it directly here.
MODEL_NAME = "qwen-2.5-32b"

# Define custom headers explicitly
custom_headers = {
    "Authorization": f"Bearer {API_KEY}"
}

# Create a custom OpenAI client with the custom URL, API key, and explicit headers via default_headers.
client = AsyncOpenAI(
    base_url=BASE_URL,
    api_key=API_KEY,
    default_headers=custom_headers
)

set_tracing_disabled(disabled=False)

spanish_agent = Agent(
    name="Spanish agent",
    instructions="You only speak Spanish.",
    model=OpenAIResponsesModel(model=MODEL_NAME, openai_client=client)
)

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
    model=OpenAIResponsesModel(model=MODEL_NAME, openai_client=client)
)

triage_agent = Agent(
    name="Triage agent",
    instructions="Handoff to the appropriate agent based on the language of the request.",
    handoffs=[spanish_agent, english_agent],
    model=OpenAIResponsesModel(model=MODEL_NAME, openai_client=client)
)

async def main():
    result = await Runner.run(triage_agent, input="Hola, ¿cómo estás?")
    print(result.final_output)
    # Expected output: "¡Hola! Estoy bien, gracias por preguntar. ¿Y tú, cómo estás?"

if __name__ == "__main__":
    asyncio.run(main())