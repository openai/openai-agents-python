import asyncio
import sys
import os

# Add project root to Python path to ensure src package can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

# Import required modules
from src.agents import Agent, Runner
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.azure_openai_provider import AzureOpenAIProvider

async def main():
    # 创建运行配置
    run_config = RunConfig()
    
    # 直接创建提供程序，它会自动从环境变量读取配置
    run_config.model_provider = AzureOpenAIProvider()
    
    # Create Azure OpenAI model settings
    azure_settings = ModelSettings(
        provider="azure_openai",  # 指定 Azure OpenAI 作为提供程序
        temperature=0.7  # 可选：控制创造性
    )

    # 创建 Agent 实例
    agent = Agent(
        name="Assistant",
        instructions="You only respond in haikus.",
        model_settings=azure_settings  # 使用 Azure OpenAI 设置
    )
    
    # 运行 Agent
    print("Running Agent with Azure OpenAI, please wait...")
    result = await Runner.run(
        agent, 
        "Tell me about cloud computing.", 
        run_config=run_config
    )
    
    # 打印结果
    print("\nResult:")
    print(result.final_output)
    # 预期输出类似于:
    # Servers in the sky,
    # Data flows through distant clouds,
    # No hardware to buy.

if __name__ == "__main__":
    # 打印使用说明
    print("Azure OpenAI Example")
    print("===================")
    print("This example requires Azure OpenAI credentials.")
    print("Make sure you have set these environment variables:")
    print("- AZURE_OPENAI_API_KEY: Your Azure OpenAI API key")
    print("- AZURE_OPENAI_ENDPOINT: Your Azure OpenAI endpoint URL")
    print("- AZURE_OPENAI_BASE_URL: (Optional) Alternative complete base URL (overrides AZURE_OPENAI_ENDPOINT)")
    print("- AZURE_OPENAI_API_VERSION: (Optional) API version (defaults to 2023-05-15)")
    print("- AZURE_OPENAI_DEPLOYMENT: (Optional) Deployment name (defaults to gpt-4o)")
    print()
    
    # 运行主函数
    asyncio.run(main())
