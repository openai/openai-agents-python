import asyncio
import os
from . import common_patches
from openai import AsyncOpenAI
from agents import Agent, Runner, set_tracing_disabled
from agents.models.openai_responses import OpenAIResponsesModel
from examples.open_responses_built_in_tools import OpenResponsesBuiltInTools

"""
This example demonstrates how to create an agent that uses the built-in think tool to perform a sequence of thinking using Anthropics's Clause -3.7 Snonnet 
model with Open Responses API.
"""

# Set custom parameters.
BASE_URL = os.getenv("OPEN_RESPONSES_URL") or "http://localhost:8080/v1" #Either set OPEN_RESPONSES_URL in environment variable or put it directly here.
API_KEY = os.getenv("CLAUDE_API_KEY") or "" #Either set GROQ_API_KEY in environment variable or put it directly here.
MODEL_NAME = "claude-3-7-sonnet-20250219"

# Define custom headers.
custom_headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}",
    "x-model-provider": "claude"
}

# Create a custom OpenAI client.
client = AsyncOpenAI(
    base_url=BASE_URL,
    api_key=API_KEY,
    default_headers=custom_headers
)

set_tracing_disabled(disabled=False)

# Instantiate the custom think tool with tool_name "think".
think_tool = OpenResponsesBuiltInTools(tool_name="think")

# Create the agent.
claude_agent_with_think_tool = Agent(
    name="Claude Agent with Think Tool",
    instructions=(
        "You are an experienced system design architect. Use the think tool to cross confirm thoughts before preparing the final answer."
    ),
    tools=[think_tool],
    model=OpenAIResponsesModel(model=MODEL_NAME, openai_client=client)
)

async def main():
    # Since the conversation is embedded in the instructions, we pass an empty input.
    result = await Runner.run(claude_agent_with_think_tool, input="Give me the guidelines on designing a multi-agent distributed system with the following constraints in mind: 1. compute costs minimal, 2. the system should be horizontally scalable, 3. the behavior should be deterministic.")
    print("Final output:", result.final_output)

if __name__ == "__main__":
    asyncio.run(main())