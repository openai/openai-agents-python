import asyncio
import sys
import os
import random
from typing import Any

# Add project root to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from pydantic import BaseModel

from src.agents import Agent, AgentHooks, RunContextWrapper, Runner, Tool, function_tool
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory


class CustomAgentHooks(AgentHooks):
    def __init__(self, display_name: str):
        self.event_counter = 0
        self.display_name = display_name

    async def on_start(self, context: RunContextWrapper, agent: Agent) -> None:
        self.event_counter += 1
        print(f"### ({self.display_name}) {self.event_counter}: Agent {agent.name} started")

    async def on_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        self.event_counter += 1
        print(
            f"### ({self.display_name}) {self.event_counter}: Agent {agent.name} ended with output {output}"
        )

    async def on_handoff(self, context: RunContextWrapper, agent: Agent, source: Agent) -> None:
        self.event_counter += 1
        print(
            f"### ({self.display_name}) {self.event_counter}: Agent {source.name} handed off to {agent.name}"
        )

    async def on_tool_start(self, context: RunContextWrapper, agent: Agent, tool: Tool) -> None:
        self.event_counter += 1
        print(
            f"### ({self.display_name}) {self.event_counter}: Agent {agent.name} started tool {tool.name}"
        )

    async def on_tool_end(
        self, context: RunContextWrapper, agent: Agent, tool: Tool, result: str
    ) -> None:
        self.event_counter += 1
        print(
            f"### ({self.display_name}) {self.event_counter}: Agent {agent.name} ended tool {tool.name} with result {result}"
        )


@function_tool
def random_number(max: int) -> int:
    """
    Generate a random number up to the provided maximum.
    """
    return random.randint(0, max)


@function_tool
def multiply_by_two(x: int) -> int:
    """Simple multiplication by two."""
    return x * 2


class FinalResult(BaseModel):
    number: int


async def main() -> None:
    # Create Ollama model settings
    ollama_settings = ModelSettings(
        provider="ollama",
        ollama_base_url="http://localhost:11434",
        ollama_default_model="llama3.2",
        temperature=0.7
    )
    # Create runtime configuration
    run_config = RunConfig(tracing_disabled=True)
    # Set model provider
    run_config.model_provider = ModelProviderFactory.create_provider(ollama_settings)

    multiply_agent = Agent(
        name="Multiply Agent",
        instructions="Multiply the number by 2 and then return the final result.",
        tools=[multiply_by_two],
        output_type=FinalResult,
        hooks=CustomAgentHooks(display_name="Multiply Agent"),
        model_settings=ollama_settings
    )

    start_agent = Agent(
        name="Start Agent",
        instructions="Generate a random number. If it's even, stop. If it's odd, hand off to the multiply agent.",
        tools=[random_number],
        output_type=FinalResult,
        handoffs=[multiply_agent],
        hooks=CustomAgentHooks(display_name="Start Agent"),
        model_settings=ollama_settings
    )

    print("Running agent lifecycle example with Ollama, please wait...")
    user_input = input("Enter a max number: ")
    await Runner.run(
        start_agent,
        input=f"Generate a random number between 0 and {user_input}.",
        run_config=run_config
    )

    print("Done!")


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
