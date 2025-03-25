import asyncio
import sys
import os
from typing import Any, Literal

# 添加项目根路径到Python路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from pydantic import BaseModel

from src.agents import (
    Agent,
    FunctionToolResult,
    ModelSettings,
    RunContextWrapper,
    Runner,
    ToolsToFinalOutputFunction,
    ToolsToFinalOutputResult,
    function_tool,
)
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

"""
此示例展示了如何强制代理使用工具。它使用`ModelSettings(tool_choice="required")`
来强制代理使用任何工具。

您可以使用3个选项运行它:
1. `default`: 默认行为，将工具输出发送到LLM。在这种情况下，
    `tool_choice`未设置，因为否则会导致无限循环 - LLM会调用工具，
    工具会运行并将结果发送到LLM，这会重复（因为模型每次都被强制使用工具。）
2. `first_tool`: 第一个工具结果被用作最终输出。
3. `custom`: 使用自定义工具使用行为函数。自定义函数接收所有工具结果，
    并选择使用第一个工具结果生成最终输出。

使用方法:
python forcing_tool_use.py -t default
python forcing_tool_use.py -t first_tool
python forcing_tool_use.py -t custom
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


class Weather(BaseModel):
    city: str
    temperature_range: str
    conditions: str


@function_tool
def get_weather(city: str) -> Weather:
    print("[调试] get_weather被调用")
    return Weather(city=city, temperature_range="14-20C", conditions="晴朗有风")


async def custom_tool_use_behavior(
    context: RunContextWrapper[Any], results: list[FunctionToolResult]
) -> ToolsToFinalOutputResult:
    weather: Weather = results[0].output
    return ToolsToFinalOutputResult(
        is_final_output=True, final_output=f"{weather.city}天气{weather.conditions}。"
    )


async def main(tool_use_behavior: Literal["default", "first_tool", "custom"] = "default"):
    print(f"使用Ollama运行强制工具使用示例，模式: {tool_use_behavior}")
    
    if tool_use_behavior == "default":
        behavior: Literal["run_llm_again", "stop_on_first_tool"] | ToolsToFinalOutputFunction = (
            "run_llm_again"
        )
    elif tool_use_behavior == "first_tool":
        behavior = "stop_on_first_tool"
    elif tool_use_behavior == "custom":
        behavior = custom_tool_use_behavior

    # 在default模式下不需要设置tool_choice，因为它会导致无限循环
    settings = create_ollama_settings()
    if tool_use_behavior != "default":
        settings.tool_choice = "required"
        
    agent = Agent(
        name="天气代理",
        instructions="您是一个有帮助的代理。",
        tools=[get_weather],
        tool_use_behavior=behavior,
        model_settings=settings
    )

    result = await Runner.run(agent, input="东京的天气怎么样？", run_config=run_config)
    print(f"结果: {result.final_output}")


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
        
    # 解析命令行参数
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-t",
        "--tool-use-behavior",
        type=str,
        default="default",
        choices=["default", "first_tool", "custom"],
        help="工具使用行为。default将工具输出发送到模型。"
        "first_tool将第一个工具结果用作最终输出。"
        "custom将使用自定义工具使用行为函数。",
    )
    args = parser.parse_args()
    
    # 运行主函数
    asyncio.run(main(args.tool_use_behavior))
