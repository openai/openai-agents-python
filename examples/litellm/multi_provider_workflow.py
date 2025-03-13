"""
This example demonstrates using multiple LLM providers in a workflow using LiteLLM.
It creates a workflow where:
1. A triage agent (using OpenAI directly) determines the task type
2. Based on the task type, it routes to:
   - A summarization agent using Claude via LiteLLM
   - A coding agent using GPT-4 via LiteLLM
   - A creative agent using Gemini via LiteLLM
"""

import asyncio
import os
from typing import Literal

from agents import Agent, Runner, OpenAIProvider, LiteLLMProvider, RunConfig
from agents.agent_output import AgentOutputSchema
from pydantic import BaseModel


class TaskType(BaseModel):
    """The type of task to be performed."""
    task: Literal["summarize", "code", "creative"]
    explanation: str


class TaskOutput(BaseModel):
    """The output of the task."""
    result: str
    provider_used: str


# Set up providers
openai_provider = OpenAIProvider(
    api_key=os.getenv("OPENAI_API_KEY")
)

litellm_provider = LiteLLMProvider(
    api_key=os.getenv("LITELLM_API_KEY"),
    base_url=os.getenv("LITELLM_API_BASE", "http://localhost:8000")
)

# Create specialized agents for different tasks
triage_agent = Agent(
    name="Triage Agent",
    instructions="""
    You are a triage agent that determines the type of task needed.
    - For text analysis, summarization, or understanding tasks, choose 'summarize'
    - For programming, coding, or technical tasks, choose 'code'
    - For creative writing, storytelling, or artistic tasks, choose 'creative'
    """,
    model="gpt-3.5-turbo",
    output_schema=AgentOutputSchema(TaskType),
)

summarize_agent = Agent(
    name="Summarization Agent",
    instructions="""
    You are a summarization expert using Claude's advanced comprehension capabilities.
    Provide clear, concise summaries while preserving key information.
    Always include "Provider Used: Anthropic Claude" in your response.
    """,
    model="claude-3",  # Will be routed to Anthropic
    output_schema=AgentOutputSchema(TaskOutput),
)

code_agent = Agent(
    name="Coding Agent",
    instructions="""
    You are a coding expert using GPT-4's technical capabilities.
    Provide clean, well-documented code solutions.
    Always include "Provider Used: OpenAI GPT-4" in your response.
    """,
    model="gpt-4",  # Will be routed to OpenAI
    output_schema=AgentOutputSchema(TaskOutput),
)

creative_agent = Agent(
    name="Creative Agent",
    instructions="""
    You are a creative writer using Gemini's creative capabilities.
    Create engaging, imaginative content.
    Always include "Provider Used: Google Gemini" in your response.
    """,
    model="gemini-pro",  # Will be routed to Google
    output_schema=AgentOutputSchema(TaskOutput),
)


async def process_request(user_input: str) -> str:
    """Process a user request using the appropriate agent."""
    
    # First, triage the request with OpenAI provider
    openai_config = RunConfig(model_provider=openai_provider)
    triage_result = await Runner.run(
        triage_agent,
        input=f"What type of task is this request? {user_input}",
        run_config=openai_config
    )
    task_type = triage_result.output
    
    # Route to the appropriate agent with LiteLLM provider
    target_agent = {
        "summarize": summarize_agent,
        "code": code_agent,
        "creative": creative_agent,
    }[task_type.task]
    
    # Process with the specialized agent using LiteLLM provider
    litellm_config = RunConfig(model_provider=litellm_provider)
    result = await Runner.run(
        target_agent, 
        input=user_input,
        run_config=litellm_config
    )
    
    return f"""
Task Type: {task_type.task}
Reason: {task_type.explanation}
Result: {result.output.result}
Provider Used: {result.output.provider_used}
"""


async def main():
    """Run example requests through the workflow."""
    requests = [
        "Can you summarize the key points of the French Revolution?",
        "Write a Python function to calculate the Fibonacci sequence.",
        "Write a short story about a time-traveling coffee cup.",
    ]
    
    for request in requests:
        print(f"\nProcessing request: {request}")
        print("-" * 80)
        result = await process_request(request)
        print(result)


if __name__ == "__main__":
    asyncio.run(main()) 