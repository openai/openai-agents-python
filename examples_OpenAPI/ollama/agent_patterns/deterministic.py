import asyncio
import sys
import os

# 添加项目根路径到Python路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from pydantic import BaseModel

from src.agents import Agent, Runner, trace
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

"""
这个示例演示了一个确定性流程，其中每个步骤由一个代理执行。
1. 第一个代理生成故事大纲
2. 我们将大纲输入到第二个代理
3. 第二个代理检查大纲是否高质量且是否为科幻故事
4. 如果大纲质量不高或不是科幻故事，我们在此停止
5. 如果大纲质量高且为科幻故事，我们将大纲输入到第三个代理
6. 第三个代理撰写故事
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

story_outline_agent = Agent(
    name="story_outline_agent",
    instructions="根据用户的输入生成非常简短的故事大纲。",
    model_settings=create_ollama_settings()
)


class OutlineCheckerOutput(BaseModel):
    good_quality: bool
    is_scifi: bool


outline_checker_agent = Agent(
    name="outline_checker_agent",
    instructions="阅读给定的故事大纲，并判断质量。同时，确定它是否是科幻故事。",
    output_type=OutlineCheckerOutput,
    model_settings=create_ollama_settings()
)

story_agent = Agent(
    name="story_agent",
    instructions="根据给定的大纲写一个短篇故事。",
    output_type=str,
    model_settings=create_ollama_settings()
)


async def main():
    input_prompt = input("您想要什么样的故事？ ")

    print("使用Ollama运行确定性流程示例，请稍候...")

    # 确保整个工作流是单一跟踪
    with trace("Deterministic story flow"):
        # 1. 生成大纲
        outline_result = await Runner.run(
            story_outline_agent,
            input_prompt,
            run_config=run_config
        )
        print("大纲已生成")
        print(f"\n故事大纲:\n{outline_result.final_output}\n")

        # 2. 检查大纲
        outline_checker_result = await Runner.run(
            outline_checker_agent,
            outline_result.final_output,
            run_config=run_config
        )

        # 3. 添加门控以停止，如果大纲质量不高或不是科幻故事
        assert isinstance(outline_checker_result.final_output, OutlineCheckerOutput)
        if not outline_checker_result.final_output.good_quality:
            print("大纲质量不高，我们在这里停止。")
            return

        if not outline_checker_result.final_output.is_scifi:
            print("大纲不是科幻故事，我们在这里停止。")
            return

        print("大纲质量高且是科幻故事，我们继续撰写故事。")

        # 4. 撰写故事
        story_result = await Runner.run(
            story_agent,
            outline_result.final_output,
            run_config=run_config
        )
        print(f"\n故事:\n{story_result.final_output}")


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
