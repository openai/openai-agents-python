import asyncio
import sys
import os
import random
from typing import Any

# 添加项目根路径到Python路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from pydantic import BaseModel

from src.agents import Agent, RunContextWrapper, RunHooks, Runner, Tool, Usage, function_tool
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory


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


@function_tool
def random_number(max: int) -> int:
    """Generate a random number up to the provided max."""
    return random.randint(0, max)


@function_tool
def multiply_by_two(x: int) -> int:
    """Return x times two."""
    return x * 2


class FinalResult(BaseModel):
    number: int


async def main() -> None:
    # 创建Ollama模型设置
    ollama_settings = ModelSettings(
        provider="ollama",
        ollama_base_url="http://localhost:11434",
        ollama_default_model="llama3.2",
        temperature=0.7
    )
    # 创建运行配置
    run_config = RunConfig(tracing_disabled=True)
    # 设置模型提供商
    run_config.model_provider = ModelProviderFactory.create_provider(ollama_settings)

    multiply_agent = Agent(
        name="Multiply Agent",
        instructions="Multiply the number by 2 and then return the final result.",
        tools=[multiply_by_two],
        output_type=FinalResult,
        model_settings=ollama_settings
    )

    start_agent = Agent(
        name="Start Agent",
        instructions="Generate a random number and then invoke the Multiply Agent.",
        tools=[random_number],
        output_type=FinalResult,
        handoffs=[multiply_agent],
        model_settings=ollama_settings
    )

    print("Running lifecycle example with Ollama, please wait...")
    user_input = input("Enter a max number: ")
    await Runner.run(
        start_agent,
        hooks=hooks,
        input=f"Generate a random number between 0 and {user_input}.",
        run_config=run_config
    )

    print("Done!")


if __name__ == "__main__":
    # 检查Ollama服务是否运行
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
        
    # 运行主函数
    asyncio.run(main())
