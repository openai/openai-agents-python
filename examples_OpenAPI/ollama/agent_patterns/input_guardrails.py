import asyncio
import sys
import os

# 添加项目根路径到Python路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from pydantic import BaseModel

from src.agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    TResponseInputItem,
    input_guardrail,
)
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

"""
这个示例展示了如何使用输入保障。

保障是与代理执行并行运行的检查。可用于：
- 检查输入消息是否偏离主题
- 检查输出消息是否违反任何政策
- 如果检测到意外输入，接管代理执行的控制权

在此示例中，我们设置一个输入保障，当用户要求帮助解决数学作业时触发。
如果保障触发，我们将用拒绝消息作为响应。
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

### 1. 基于代理的保障，当用户要求解数学作业时触发
class MathHomeworkOutput(BaseModel):
    reasoning: str
    is_math_homework: bool


guardrail_agent = Agent(
    name="Guardrail check",
    instructions="检查用户是否要求您做他们的数学作业。",
    output_type=MathHomeworkOutput,
    model_settings=create_ollama_settings()
)


@input_guardrail
async def math_guardrail(
    context: RunContextWrapper[None], agent: Agent, input: str | list[TResponseInputItem]
) -> GuardrailFunctionOutput:
    """这是一个输入保障函数，它调用一个代理来检查输入是否是数学作业问题。"""
    result = await Runner.run(guardrail_agent, input, context=context.context, run_config=run_config)
    final_output = result.final_output_as(MathHomeworkOutput)

    return GuardrailFunctionOutput(
        output_info=final_output,
        tripwire_triggered=final_output.is_math_homework,
    )


### 2. 运行循环


async def main():
    agent = Agent(
        name="客户支持代理",
        instructions="您是一个客户支持代理。您帮助客户解答他们的问题。",
        input_guardrails=[math_guardrail],
        model_settings=create_ollama_settings()
    )

    input_data: list[TResponseInputItem] = []

    print("使用Ollama运行输入保障示例")
    print("尝试提问普通问题，然后尝试提出数学作业问题（如'帮我解方程：2x + 5 = 11'）")
    print("输入'exit'退出")
    
    while True:
        user_input = input("\n请输入消息: ")
        if user_input.lower() == 'exit':
            break
            
        input_data.append(
            {
                "role": "user",
                "content": user_input,
            }
        )

        print("处理中...")
        try:
            result = await Runner.run(agent, input_data, run_config=run_config)
            print(result.final_output)
            # 如果保障未触发，使用结果作为下一次运行的输入
            input_data = result.to_input_list()
        except InputGuardrailTripwireTriggered:
            # 如果保障触发，添加拒绝消息到输入
            message = "抱歉，我不能帮您解决数学作业。"
            print(message)
            input_data.append(
                {
                    "role": "assistant",
                    "content": message,
                }
            )


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
