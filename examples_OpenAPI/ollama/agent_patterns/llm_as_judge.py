import asyncio
import sys
import os
from dataclasses import dataclass
from typing import Literal

# 添加项目根路径到Python路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from src.agents import Agent, ItemHelpers, Runner, TResponseInputItem, trace
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

"""
这个示例展示了LLM作为裁判模式。第一个代理生成故事大纲，第二个代理评估大纲并提供反馈。
我们循环直到裁判满意为止。
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

story_outline_generator = Agent(
    name="story_outline_generator",
    instructions=(
        "您根据用户的输入生成一个非常简短的故事大纲。"
        "如果提供了任何反馈，请使用它来改进大纲。"
    ),
    model_settings=create_ollama_settings()
)


@dataclass
class EvaluationFeedback:
    feedback: str
    score: Literal["pass", "needs_improvement", "fail"]


evaluator = Agent[None](
    name="evaluator",
    instructions=(
        "您评估一个故事大纲并决定它是否足够好。"
        "如果不够好，您提供关于需要改进什么的反馈。"
        "永远不要在第一次尝试时给它通过。"
    ),
    output_type=EvaluationFeedback,
    model_settings=create_ollama_settings()
)


async def main() -> None:
    msg = input("您想听什么样的故事？ ")
    input_items: list[TResponseInputItem] = [{"content": msg, "role": "user"}]

    latest_outline: str | None = None

    print("使用Ollama运行LLM作为裁判示例，请稍候...")

    # 我们将整个工作流运行在一个跟踪中
    with trace("LLM as a judge"):
        iteration = 1
        while True:
            print(f"\n--- 迭代 {iteration} ---")
            story_outline_result = await Runner.run(
                story_outline_generator,
                input_items,
                run_config=run_config
            )

            input_items = story_outline_result.to_input_list()
            latest_outline = ItemHelpers.text_message_outputs(story_outline_result.new_items)
            print(f"生成的故事大纲:\n{latest_outline}")

            evaluator_result = await Runner.run(evaluator, input_items, run_config=run_config)
            result: EvaluationFeedback = evaluator_result.final_output

            print(f"评估结果: {result.score}")
            print(f"评估反馈: {result.feedback}")

            if result.score == "pass":
                print("故事大纲已经足够好，退出循环。")
                break

            print("根据反馈重新运行...")
            input_items.append({"content": f"反馈: {result.feedback}", "role": "user"})
            iteration += 1

    print(f"\n最终故事大纲:\n{latest_outline}")


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
