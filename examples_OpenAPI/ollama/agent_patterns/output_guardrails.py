import asyncio
import sys
import os
import json

# 添加项目根路径到Python路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from pydantic import BaseModel, Field

from src.agents import (
    Agent,
    GuardrailFunctionOutput,
    OutputGuardrailTripwireTriggered,
    RunContextWrapper,
    Runner,
    output_guardrail,
)
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

"""
这个示例展示了如何使用输出保障。

输出保障是对代理最终输出运行的检查。可用于：
- 检查输出是否包含敏感数据
- 检查输出是否是对用户消息的有效响应

在此示例中，我们使用（人为构造的）例子，检查代理的响应是否包含电话号码。
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

# 代理的输出类型
class MessageOutput(BaseModel):
    reasoning: str = Field(description="关于如何回应用户消息的思考")
    response: str = Field(description="对用户消息的回应")
    user_name: str | None = Field(description="发送消息的用户名称，如果已知")


@output_guardrail
async def sensitive_data_check(
    context: RunContextWrapper, agent: Agent, output: MessageOutput
) -> GuardrailFunctionOutput:
    phone_number_in_response = "650" in output.response
    phone_number_in_reasoning = "650" in output.reasoning

    return GuardrailFunctionOutput(
        output_info={
            "phone_number_in_response": phone_number_in_response,
            "phone_number_in_reasoning": phone_number_in_reasoning,
        },
        tripwire_triggered=phone_number_in_response or phone_number_in_reasoning,
    )


agent = Agent(
    name="助手",
    instructions="您是一个有帮助的助手。",
    output_type=MessageOutput,
    output_guardrails=[sensitive_data_check],
    model_settings=create_ollama_settings()
)


async def main():
    print("使用Ollama运行输出保障示例")
    
    # 这应该没问题
    print("测试普通问题...")
    result1 = await Runner.run(agent, "加利福尼亚的首都是什么？", run_config=run_config)
    print("第一条消息通过")
    print(f"输出: {json.dumps(result1.final_output.model_dump(), indent=2, ensure_ascii=False)}")

    print("\n测试包含电话号码的问题...")
    # 这应该会触发保障
    try:
        result2 = await Runner.run(
            agent, "我的电话号码是650-123-4567。你认为我住在哪里？", run_config=run_config
        )
        print(
            f"保障未触发 - 这是意外的。输出: {json.dumps(result2.final_output.model_dump(), indent=2, ensure_ascii=False)}"
        )

    except OutputGuardrailTripwireTriggered as e:
        print(f"保障已触发。信息: {e.guardrail_result.output.output_info}")


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
