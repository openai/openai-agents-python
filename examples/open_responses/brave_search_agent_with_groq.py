import asyncio
import os
from . import common_patches
from openai import AsyncOpenAI
from agents import Agent, Runner, set_tracing_disabled
from agents.models.openai_responses import OpenAIResponsesModel
from examples.open_responses_built_in_tools import OpenResponsesBuiltInTools

"""
This example demonstrates how to create an agent that uses the built-in brave_web_search tool to perform a web search using Groq model with Open Responses API.
"""

BASE_URL = os.getenv("OPEN_RESPONSES_URL") or "http://localhost:8080/v1" #Either set OPEN_RESPONSES_URL in environment variable or put it directly here.
API_KEY = os.getenv("GROQ_API_KEY") or "" #Either set GROQ_API_KEY in environment variable or put it directly here.
MODEL_NAME = "qwen-2.5-32b"

custom_headers = {
    "Authorization": f"Bearer {API_KEY}"
}

client = AsyncOpenAI(
    base_url=BASE_URL,
    api_key=API_KEY,
    default_headers=custom_headers
)

set_tracing_disabled(disabled=False)

brave_search_tool = OpenResponsesBuiltInTools(tool_name="brave_web_search")

search_agent = Agent(
    name="Brave Search Agent",
    instructions=(
        "You are a research assistant that uses Brave web search. "
        "When given a query, perform a web search using Brave and provide a concise summary."
    ),
    tools=[brave_search_tool],
    model=OpenAIResponsesModel(model=MODEL_NAME, openai_client=client)
)

async def main():
    query = "Where did NVIDIA GTC happen in 2025 and what were the major announcements?"
    result = await Runner.run(search_agent, input=query)
    print("Final output:", result.final_output)

if __name__ == "__main__":
    asyncio.run(main())