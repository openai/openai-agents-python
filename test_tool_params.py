#!/usr/bin/env python3
"""
测试脚本：验证 streaming_tool 参数传递是否正常工作
"""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

from openai import AsyncOpenAI

from agents import (
    Agent,
    NotifyStreamEvent,
    Runner,
    StreamEvent,
    ToolStreamStartEvent,
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
    streaming_tool,
)

# --- 配置API端点信息 ---
OPENAI_API_KEY = "d0c53e7f-d115-44af-b58c-51bb8fca6ac7"
OPENAI_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
MODEL_NAME = "deepseek-v3-250324"

# 设置默认的 OpenAI 客户端
client = AsyncOpenAI(
    base_url=OPENAI_BASE_URL,
    api_key=OPENAI_API_KEY,
)
set_default_openai_client(client)
set_default_openai_api("chat_completions")
set_tracing_disabled(disabled=True)


@streaming_tool
async def greet_person(name: str, times: int = 1) -> AsyncGenerator[StreamEvent | str, Any]:
    """向指定的人问好指定次数。
    
    Args:
        name: 要问候的人的名字
        times: 问候的次数，默认为1次
    """
    yield NotifyStreamEvent(data=f"开始向 {name} 问好 {times} 次...")
    
    for i in range(times):
        yield NotifyStreamEvent(data=f"第 {i+1} 次：你好，{name}！")
        await asyncio.sleep(0.01)
    
    yield f"已成功向 {name} 问好 {times} 次"


async def test_explicit_params():
    """测试明确指定参数的情况"""
    print("=== 测试1：明确指定参数 ===")
    
    agent = Agent(
        name="问候代理",
        instructions=(
            "你必须使用 greet_person 工具。"
            "当用户说'向张三问好2次'时，你必须调用 greet_person(name='张三', times=2)。"
            "当用户说'向李四问好'时，你必须调用 greet_person(name='李四', times=1)。"
            "严格按照用户的要求传递正确的参数。"
        ),
        model=MODEL_NAME,
        tools=[greet_person],
    )
    
    # 测试用例1：明确指定次数
    print("\n--- 测试用例1：向张三问好2次 ---")
    result = Runner.run_streamed(agent, input="向张三问好2次")
    
    tool_calls = []
    async for event in result.stream_events():
        if isinstance(event, ToolStreamStartEvent):
            tool_calls.append(event)
            print(f"工具调用: {event.tool_name}")
            print(f"参数: {event.input_args}")
    
    final_output = result.final_output
    print(f"最终输出: {final_output}")
    
    # 验证参数是否正确传递
    if tool_calls:
        args = tool_calls[0].input_args
        if args.get('name') == '张三' and args.get('times') == 2:
            print("✅ 参数传递正确")
        else:
            print(f"❌ 参数传递错误: {args}")
    else:
        print("❌ 没有工具调用")
    
    # 测试用例2：使用默认次数
    print("\n--- 测试用例2：向李四问好（默认次数） ---")
    result = Runner.run_streamed(agent, input="向李四问好")
    
    tool_calls = []
    async for event in result.stream_events():
        if isinstance(event, ToolStreamStartEvent):
            tool_calls.append(event)
            print(f"工具调用: {event.tool_name}")
            print(f"参数: {event.input_args}")
    
    final_output = result.final_output
    print(f"最终输出: {final_output}")
    
    # 验证参数是否正确传递
    if tool_calls:
        args = tool_calls[0].input_args
        if args.get('name') == '李四' and args.get('times') in [1, None]:  # times可能是默认值
            print("✅ 参数传递正确")
        else:
            print(f"❌ 参数传递错误: {args}")
    else:
        print("❌ 没有工具调用")


async def main():
    """主函数"""
    print("开始测试 streaming_tool 参数传递...")
    await test_explicit_params()
    print("\n测试完成！")


if __name__ == "__main__":
    asyncio.run(main())
