import asyncio
import sys
import os

# 添加项目根路径到Python路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from src.agents import Agent, ItemHelpers, MessageOutputItem, Runner, trace
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

"""
这个示例展示了代理作为工具模式。前线代理接收用户消息，然后选择调用哪些代理作为工具。
在这个例子中，它从一组翻译代理中进行选择。
"""

def create_ollama_settings(model="phi3:latest"):
    """创建Ollama模型设置"""
    return ModelSettings(
        provider="ollama",
        ollama_base_url="http://localhost:11434",
        ollama_default_model=model,
        temperature=0.7
    )

# 创建运行配置
run_config = RunConfig(tracing_disabled=True)
# 设置模型提供商
run_config.model_provider = ModelProviderFactory.create_provider(create_ollama_settings())

spanish_agent = Agent(
    name="spanish_agent",
    instructions="将用户的消息翻译成西班牙语",
    handoff_description="一个英语到西班牙语翻译器",
    model_settings=create_ollama_settings()
)

french_agent = Agent(
    name="french_agent",
    instructions="将用户的消息翻译成法语",
    handoff_description="一个英语到法语翻译器",
    model_settings=create_ollama_settings()
)

italian_agent = Agent(
    name="italian_agent",
    instructions="将用户的消息翻译成意大利语",
    handoff_description="一个英语到意大利语翻译器",
    model_settings=create_ollama_settings()
)

orchestrator_agent = Agent(
    name="orchestrator_agent",
    instructions=(
        "您是一个翻译代理。您使用提供给您的工具进行翻译。"
        "如果被要求进行多种翻译，您按顺序调用相关工具。"
        "您永远不要自己翻译，总是使用提供的工具。"
    ),
    tools=[
        spanish_agent.as_tool(
            tool_name="translate_to_spanish",
            tool_description="将用户的消息翻译成西班牙语",
        ),
        french_agent.as_tool(
            tool_name="translate_to_french",
            tool_description="将用户的消息翻译成法语",
        ),
        italian_agent.as_tool(
            tool_name="translate_to_italian",
            tool_description="将用户的消息翻译成意大利语",
        ),
    ],
    model_settings=create_ollama_settings()
)

synthesizer_agent = Agent(
    name="synthesizer_agent",
    instructions="您检查翻译，必要时进行更正，并生成最终的连接响应。",
    model_settings=create_ollama_settings()
)


async def main():
    msg = input("您好！您想要翻译什么内容，以及翻译成哪些语言？ ")

    print("使用Ollama运行代理作为工具示例，请稍候...")

    # 在单个跟踪中运行整个编排
    with trace("Orchestrator evaluator"):
        orchestrator_result = await Runner.run(
            orchestrator_agent, 
            msg,
            run_config=run_config
        )

        for item in orchestrator_result.new_items:
            if isinstance(item, MessageOutputItem):
                text = ItemHelpers.text_message_output(item)
                if text:
                    print(f"  - 翻译步骤: {text}")

        synthesizer_result = await Runner.run(
            synthesizer_agent, 
            orchestrator_result.to_input_list(),
            run_config=run_config
        )

    print(f"\n\n最终响应:\n{synthesizer_result.final_output}")


if __name__ == "__main__":
    # 检查Ollama服务是否运行
    import httpx
    try:
        response = httpx.get("http://localhost:11434/api/tags")
        if response.status_code != 200:
            print("错误: Ollama服务返回非200状态码。请确保Ollama服务正在运行。")
            sys.exit(1)
    except Exception as e:
        print(f"错误: 无法连接到Ollama服务。请确保Ollama服务正在运行。\n{str(e)}")
        print("\n如果您尚未安装Ollama，请从https://ollama.ai下载并安装，然后运行'ollama serve'启动服务")
        sys.exit(1)
        
    # 运行主函数
    asyncio.run(main())
