import asyncio
import sys
import os
import uuid

# 添加项目根路径到Python路径
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../..')))

from openai.types.responses import ResponseContentPartDoneEvent, ResponseTextDeltaEvent

from src.agents import Agent, RawResponsesStreamEvent, Runner, TResponseInputItem, trace
from src.agents.model_settings import ModelSettings
from src.agents.run import RunConfig
from src.agents.models.provider_factory import ModelProviderFactory

"""
这个示例展示了分流/路由模式。分流代理收到第一条消息，然后根据请求的语言移交给适当的代理。
响应以流式方式传送给用户。
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

french_agent = Agent(
    name="french_agent",
    instructions="You only speak French",
    model_settings=create_ollama_settings()
)

spanish_agent = Agent(
    name="spanish_agent",
    instructions="You only speak Spanish",
    model_settings=create_ollama_settings()
)

english_agent = Agent(
    name="english_agent",
    instructions="You only speak English",
    model_settings=create_ollama_settings()
)

triage_agent = Agent(
    name="triage_agent",
    instructions="Handoff to the appropriate agent based on the language of the request.",
    handoffs=[french_agent, spanish_agent, english_agent],
    model_settings=create_ollama_settings()
)


async def main():
    # 我们为这个对话创建一个ID，以便链接每个跟踪
    conversation_id = str(uuid.uuid4().hex[:16])

    print("欢迎使用多语言助手！我们提供法语、西班牙语和英语服务。")
    print("请输入您的问题 (输入'exit'退出):")
    msg = input("> ")
    
    if msg.lower() == 'exit':
        return
        
    agent = triage_agent
    inputs: list[TResponseInputItem] = [{"content": msg, "role": "user"}]

    while True:
        # 每个对话回合是单个跟踪。通常，来自用户的每个输入都是对您的应用程序的API请求，
        # 您可以在trace()中包装该请求
        with trace("Routing example", group_id=conversation_id):
            result = Runner.run_streamed(
                agent,
                input=inputs,
                run_config=run_config
            )
            print("\n回复: ", end="", flush=True)
            async for event in result.stream_events():
                if not isinstance(event, RawResponsesStreamEvent):
                    continue
                data = event.data
                if isinstance(data, ResponseTextDeltaEvent):
                    print(data.delta, end="", flush=True)
                elif isinstance(data, ResponseContentPartDoneEvent):
                    print("\n")

        inputs = result.to_input_list()
        print("\n")

        user_msg = input("> ")
        if user_msg.lower() == 'exit':
            break
            
        inputs.append({"content": user_msg, "role": "user"})
        agent = result.current_agent


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
