import asyncio
import random
from typing import Any, Optional

from pydantic import BaseModel

from agents import Agent, RunContextWrapper, RunHooks, Runner, Tool, Usage, function_tool
from agents.items import ModelResponse, TResponseInputItem


class ExampleHooks(RunHooks):
    def __init__(self):
        self.event_counter = 0

    def _usage_to_str(self, usage: Usage) -> str:
        return f"{usage.requests} requests, {usage.input_tokens} input tokens, {usage.output_tokens} output tokens, {usage.total_tokens} total tokens"

    async def on_agent_start(self, context: RunContextWrapper, agent: Agent) -> None:
        self.event_counter += 1
        print(
            f"### {self.event_counter}: Agent {agent.name} started. Usage: {self._usage_to_str(context.usage)}"
        )

    async def on_llm_start(
        self,
        context: RunContextWrapper,
        agent: Agent,
        system_prompt: Optional[str],
        input_items: list[TResponseInputItem],
    ) -> None:
        self.event_counter += 1
        print(f"### {self.event_counter}: LLM started. Usage: {self._usage_to_str(context.usage)}")

    async def on_llm_end(
        self, context: RunContextWrapper, agent: Agent, response: ModelResponse
    ) -> None:
        self.event_counter += 1
        print(f"### {self.event_counter}: LLM ended. Usage: {self._usage_to_str(context.usage)}")

    async def on_agent_end(self, context: RunContextWrapper, agent: Agent, output: Any) -> None:
        self.event_counter += 1
        print(
            f"### {self.event_counter}: Agent {agent.name} ended with output {output}. Usage: {self._usage_to_str(context.usage)}"
        )

    async def on_tool_start(self, context: RunContextWrapper, agent: Agent, tool: Tool) -> None:
        self.event_counter += 1
        print(
            f"### {self.event_counter}: Tool {tool.name} started. Usage: {self._usage_to_str(context.usage)}"
        )

    async def on_tool_end(
        self, context: RunContextWrapper, agent: Agent, tool: Tool, result: str
    ) -> None:
        self.event_counter += 1
        print(
            f"### {self.event_counter}: Tool {tool.name} ended with result {result}. Usage: {self._usage_to_str(context.usage)}"
        )

    async def on_handoff(
        self, context: RunContextWrapper, from_agent: Agent, to_agent: Agent
    ) -> None:
        self.event_counter += 1
        print(
            f"### {self.event_counter}: Handoff from {from_agent.name} to {to_agent.name}. Usage: {self._usage_to_str(context.usage)}"
        )


hooks = ExampleHooks()

###


@function_tool
def random_number(max: int) -> int:
    """Generate a random number from 0 to max (inclusive)."""
    return random.randint(0, max)


@function_tool
def multiply_by_two(x: int) -> int:
    """Return x times two."""
    return x * 2


class FinalResult(BaseModel):
    number: int


multiply_agent = Agent(
    name="Multiply Agent",
    instructions="Multiply the number by 2 and then return the final result.",
    tools=[multiply_by_two],
    output_type=FinalResult,
)

start_agent = Agent(
    name="Start Agent",
    instructions="Generate a random number. If it's even, stop. If it's odd, hand off to the multiplier agent.",
    tools=[random_number],
    output_type=FinalResult,
    handoffs=[multiply_agent],
)


async def main() -> None:
    user_input = input("Enter a max number: ")
    try:
        max_number = int(user_input)
        await Runner.run(
            start_agent,
            hooks=hooks,
            input=f"Generate a random number between 0 and {max_number}.",
        )
    except ValueError:
        print("Please enter a valid integer.")
        return

    print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
"""
$ python examples/basic/lifecycle_example.py

Enter a max number: 250
### 1: Agent Start Agent started. Usage: 0 requests, 0 input tokens, 0 output tokens, 0 total tokens
### 2: LLM started. Usage: 0 requests, 0 input tokens, 0 output tokens, 0 total tokens
### 3: LLM ended. Usage: 1 requests, 143 input tokens, 15 output tokens, 158 total tokens
### 4: Tool random_number started. Usage: 1 requests, 143 input tokens, 15 output tokens, 158 total tokens
### 5: Tool random_number ended with result 69. Usage: 1 requests, 143 input tokens, 15 output tokens, 158 total tokens
### 6: LLM started. Usage: 1 requests, 143 input tokens, 15 output tokens, 158 total tokens
### 7: LLM ended. Usage: 2 requests, 310 input tokens, 29 output tokens, 339 total tokens
### 8: Handoff from Start Agent to Multiply Agent. Usage: 2 requests, 310 input tokens, 29 output tokens, 339 total tokens
### 9: Agent Multiply Agent started. Usage: 2 requests, 310 input tokens, 29 output tokens, 339 total tokens
### 10: LLM started. Usage: 2 requests, 310 input tokens, 29 output tokens, 339 total tokens
### 11: LLM ended. Usage: 3 requests, 472 input tokens, 45 output tokens, 517 total tokens
### 12: Tool multiply_by_two started. Usage: 3 requests, 472 input tokens, 45 output tokens, 517 total tokens
### 13: Tool multiply_by_two ended with result 138. Usage: 3 requests, 472 input tokens, 45 output tokens, 517 total tokens
### 14: LLM started. Usage: 3 requests, 472 input tokens, 45 output tokens, 517 total tokens
### 15: LLM ended. Usage: 4 requests, 660 input tokens, 56 output tokens, 716 total tokens
### 16: Agent Multiply Agent ended with output number=138. Usage: 4 requests, 660 input tokens, 56 output tokens, 716 total tokens
Done!

"""
