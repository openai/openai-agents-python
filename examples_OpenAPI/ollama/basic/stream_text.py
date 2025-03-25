import asyncio
import sys
import os

# 添加项目根路径到Python路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from openai.types.responses import ResponseTextDeltaEvent

from src.agents import Agent, Runner
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory


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
        instructions="You are a helpful assistant.",
        model_settings=ollama_settings
    )

    print("Streaming jokes using Ollama, please wait...")
    result = Runner.run_streamed(
        agent, 
        input="Please tell me 5 jokes.",
        run_config=run_config
    )
    async for event in result.stream_events():
        if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
            print(event.data.delta, end="", flush=True)


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
