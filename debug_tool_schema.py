#!/usr/bin/env python3
"""
调试脚本：检查 streaming_tool 的 JSON schema 生成是否正确
"""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

from agents import (
    Agent,
    NotifyStreamEvent,
    StreamEvent,
    streaming_tool,
)

# 定义一个带参数的 streaming_tool
@streaming_tool
async def test_tool_with_params(target: str, count: int = 1) -> AsyncGenerator[StreamEvent | str, Any]:
    """测试工具，接收参数并输出。
    
    Args:
        target: 要问候的目标
        count: 问候的次数，默认为1
    """
    for i in range(count):
        yield NotifyStreamEvent(data=f"第{i+1}次问候：你好，{target}！")
        await asyncio.sleep(0.01)
    yield f"已向 {target} 问候 {count} 次"


# 定义一个无参数的 streaming_tool
@streaming_tool
async def test_tool_no_params() -> AsyncGenerator[StreamEvent | str, Any]:
    """无参数测试工具。"""
    yield NotifyStreamEvent(data="无参数工具执行中...")
    yield "无参数工具执行完成"


def main():
    """检查工具的 JSON schema"""
    print("=== 调试 streaming_tool 的 JSON schema ===\n")
    
    # 检查带参数的工具
    print("1. 带参数的工具 (test_tool_with_params):")
    print(f"   名称: {test_tool_with_params.name}")
    print(f"   描述: {test_tool_with_params.description}")
    print(f"   JSON Schema:")
    import json
    print(json.dumps(test_tool_with_params.params_json_schema, indent=2, ensure_ascii=False))
    print()
    
    # 检查无参数的工具
    print("2. 无参数的工具 (test_tool_no_params):")
    print(f"   名称: {test_tool_no_params.name}")
    print(f"   描述: {test_tool_no_params.description}")
    print(f"   JSON Schema:")
    print(json.dumps(test_tool_no_params.params_json_schema, indent=2, ensure_ascii=False))
    print()
    
    # 创建 Agent 并检查其工具配置
    print("3. Agent 工具配置:")
    agent = Agent(
        name="测试代理",
        instructions="你有两个工具可用：test_tool_with_params 和 test_tool_no_params",
        tools=[test_tool_with_params, test_tool_no_params],
    )
    
    print(f"   Agent 名称: {agent.name}")
    print(f"   工具数量: {len(agent.tools)}")
    for i, tool in enumerate(agent.tools):
        print(f"   工具 {i+1}: {tool.name}")
        if hasattr(tool, 'params_json_schema'):
            print(f"     Schema: {json.dumps(tool.params_json_schema, indent=6, ensure_ascii=False)}")
    
    print("\n=== 调试完成 ===")


if __name__ == "__main__":
    main()
