import asyncio
import sys
import os
import random

# 添加项目根路径到Python路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from src.agents import Agent, ItemHelpers, Runner, function_tool
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory


@function_tool
def how_many_jokes() -> int:
    return random.randint(1, 10)


async def main():
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
    
    agent = Agent(
        name="Joker",
        instructions="First call the `how_many_jokes` tool, then tell that many jokes.",
        tools=[how_many_jokes],
        model_settings=ollama_settings
    )

    print("Streaming items using Ollama, please wait...")
    result = Runner.run_streamed(
        agent,
        input="Hello",
        run_config=run_config
    )
    print("=== Run starting ===")
    async for event in result.stream_events():
        # We'll ignore the raw responses event deltas
        if event.type == "raw_response_event":
            continue
        elif event.type == "agent_updated_stream_event":
            print(f"Agent updated: {event.new_agent.name}")
            continue
        elif event.type == "run_item_stream_event":
            if event.item.type == "tool_call_item":
                print("-- Tool was called")
            elif event.item.type == "tool_call_output_item":
                print(f"-- Tool output: {event.item.output}")
            elif event.item.type == "message_output_item":
                print(f"-- Message output:\n {ItemHelpers.text_message_output(event.item)}")
            else:
                pass  # Ignore other event types

    print("=== Run complete ===")


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
