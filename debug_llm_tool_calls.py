#!/usr/bin/env python3
"""
调试脚本：检查 LLM 实际返回的工具调用信息
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
async def debug_tool(name: str, count: int = 1) -> AsyncGenerator[StreamEvent | str, Any]:
    """调试工具，打印接收到的参数。
    
    Args:
        name: 名字参数
        count: 计数参数，默认为1
    """
    yield NotifyStreamEvent(data=f"调试工具收到参数: name='{name}', count={count}")
    yield f"参数已接收: name='{name}', count={count}"


# 创建一个自定义的 streaming_tool 来拦截参数
def create_debug_streaming_tool():
    """创建一个调试版本的 streaming_tool，可以打印参数信息"""
    from agents.tool import streaming_tool as original_streaming_tool
    from agents.function_schema import function_schema
    from agents.tool import StreamingTool
    import json
    
    def debug_streaming_tool(func):
        # 获取原始的 schema
        schema = function_schema(func)
        
        async def debug_on_invoke_tool(ctx, arguments_json, tool_call_id):
            print(f"\n=== 调试信息 ===")
            print(f"工具名称: {schema.name}")
            print(f"参数 JSON 字符串: '{arguments_json}'")
            print(f"参数类型: {type(arguments_json)}")
            print(f"参数长度: {len(arguments_json) if arguments_json else 'None'}")
            
            # 尝试解析 JSON
            try:
                if arguments_json:
                    parsed_args = json.loads(arguments_json)
                    print(f"解析后的参数: {parsed_args}")
                    print(f"解析后的参数类型: {type(parsed_args)}")
                else:
                    parsed_args = {}
                    print("参数为空，使用空字典")
            except json.JSONDecodeError as e:
                print(f"JSON 解析失败: {e}")
                parsed_args = {}
            
            print(f"工具调用 ID: {tool_call_id}")
            print("=================\n")
            
            # 调用原始工具函数
            if schema.takes_context:
                generator = func(ctx, **parsed_args)
            else:
                generator = func(**parsed_args)
            
            async for event in generator:
                yield event
        
        return StreamingTool(
            name=schema.name,
            description=schema.description or "",
            params_json_schema=schema.params_json_schema,
            on_invoke_tool=debug_on_invoke_tool,
        )
    
    return debug_streaming_tool


# 使用调试版本的装饰器
debug_streaming_tool_decorator = create_debug_streaming_tool()

@debug_streaming_tool_decorator
async def test_tool(name: str, count: int = 1) -> AsyncGenerator[StreamEvent | str, Any]:
    """测试工具。
    
    Args:
        name: 名字参数
        count: 计数参数，默认为1
    """
    yield NotifyStreamEvent(data=f"工具执行: name='{name}', count={count}")
    yield f"执行完成: name='{name}', count={count}"


async def main():
    """主函数"""
    print("开始调试 LLM 工具调用...")
    
    agent = Agent(
        name="调试代理",
        instructions=(
            "你必须使用 test_tool 工具。"
            "当用户说'测试张三3次'时，你必须调用 test_tool(name='张三', count=3)。"
            "严格按照用户的要求传递正确的参数。"
        ),
        model=MODEL_NAME,
        tools=[test_tool],
    )
    
    print("\n--- 测试用例：测试张三3次 ---")
    result = Runner.run_streamed(agent, input="测试张三3次")
    
    async for event in result.stream_events():
        if isinstance(event, ToolStreamStartEvent):
            print(f"\n收到 ToolStreamStartEvent:")
            print(f"  工具名称: {event.tool_name}")
            print(f"  输入参数: {event.input_args}")
            print(f"  参数类型: {type(event.input_args)}")
    
    final_output = result.final_output
    print(f"\n最终输出: {final_output}")


if __name__ == "__main__":
    asyncio.run(main())
