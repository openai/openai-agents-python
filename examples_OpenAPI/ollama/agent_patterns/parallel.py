import asyncio
import sys
import os

# 添加项目根路径到Python路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from src.agents import Agent, ItemHelpers, Runner, trace
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

"""
这个示例展示了并行化模式。我们并行运行代理三次，并选择最佳结果。
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
    model_settings=create_ollama_settings()
)

translation_picker = Agent(
    name="translation_picker",
    instructions="从给定的选项中选择最佳西班牙语翻译。",
    model_settings=create_ollama_settings()
)


async def main():
    msg = input("嗨！输入一条消息，我们将把它翻译成西班牙语。\n\n")

    print("使用Ollama并行运行多个翻译，请稍候...")

    # 确保整个工作流是单一跟踪
    with trace("Parallel translation"):
        res_1, res_2, res_3 = await asyncio.gather(
            Runner.run(
                spanish_agent,
                msg,
                run_config=run_config
            ),
            Runner.run(
                spanish_agent,
                msg,
                run_config=run_config
            ),
            Runner.run(
                spanish_agent,
                msg,
                run_config=run_config
            ),
        )

        outputs = [
            ItemHelpers.text_message_outputs(res_1.new_items),
            ItemHelpers.text_message_outputs(res_2.new_items),
            ItemHelpers.text_message_outputs(res_3.new_items),
        ]

        translations = "\n\n".join(outputs)
        print(f"\n\n翻译结果:\n\n{translations}")

        best_translation = await Runner.run(
            translation_picker,
            f"输入: {msg}\n\n翻译:\n{translations}",
            run_config=run_config
        )

    print("\n\n-----")

    print(f"最佳翻译: {best_translation.final_output}")


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
