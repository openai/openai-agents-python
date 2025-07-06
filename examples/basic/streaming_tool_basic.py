"""
@streaming_tool 基础入门示例

本示例专为初学者设计，展示@streaming_tool的核心概念：
1. 如何创建最简单的流式工具
2. 过程通知 vs 最终结果的区别
3. 在Agent中使用流式工具
4. 监听和处理流式事件

核心理念：yield NotifyStreamEvent(...) 用于过程展示，yield "字符串" 用于最终结果
"""
import asyncio
from collections.abc import AsyncGenerator
from typing import Any, TypedDict

from agents import Agent, NotifyStreamEvent, StreamEvent, streaming_tool
from agents.tool import StreamingTool


class DemoConfig(TypedDict):
    """演示配置的类型定义"""
    name: str
    tool: StreamingTool
    params: str
    description: str


# ============================================================================
# 示例1：最简单的流式工具
# ============================================================================

@streaming_tool
async def simple_progress_tool(task_name: str) -> AsyncGenerator[StreamEvent | str, Any]:
    """最基础的流式工具示例

    展示核心概念：
    - yield NotifyStreamEvent(...) = 过程通知，不影响对话历史
    - yield "字符串" = 最终结果，影响对话历史（必须是最后一个yield）

    Args:
        task_name: 要执行的任务名称
    """
    # 过程通知1：开始执行
    yield NotifyStreamEvent(data=f"🚀 开始执行任务: {task_name}")

    # 模拟工作步骤
    steps = ["环境初始化", "数据加载", "核心处理", "结果生成"]

    for i, step in enumerate(steps, 1):
        # 过程通知2-5：每个步骤的进度
        yield NotifyStreamEvent(data=f"[{i}/{len(steps)}] {step}中...")
        await asyncio.sleep(0.5)  # 模拟工作时间

    # 过程通知6：完成提示
    yield NotifyStreamEvent(data="✅ 所有步骤完成!", tag="success")

    # 最终结果：这是唯一会影响对话历史的输出
    yield f"任务 '{task_name}' 执行成功！共完成 {len(steps)} 个步骤，耗时 {len(steps) * 0.5} 秒。"


# ============================================================================
# 示例2：实时倒计时工具
# ============================================================================

@streaming_tool
async def countdown_tool(seconds: int) -> AsyncGenerator[StreamEvent | str, Any]:
    """倒计时工具 - 演示实时更新和标签使用

    展示概念：
    - 实时进度更新
    - 使用tag参数进行事件分类
    - 时间相关的流式输出

    Args:
        seconds: 倒计时秒数
    """
    # 开始通知
    yield NotifyStreamEvent(data=f"⏰ 开始 {seconds} 秒倒计时")

    # 倒计时循环
    for i in range(seconds, 0, -1):
        yield NotifyStreamEvent(data=f"倒计时: {i}", tag="countdown")
        await asyncio.sleep(1)

    # 完成通知
    yield NotifyStreamEvent(data="🎉 倒计时结束!", tag="complete")

    # 最终结果
    yield f"倒计时 {seconds} 秒已完成，当前时间: {asyncio.get_event_loop().time():.1f}"


# ============================================================================
# 示例3：错误处理演示
# ============================================================================

@streaming_tool
async def error_demo_tool(should_fail: bool) -> AsyncGenerator[StreamEvent | str, Any]:
    """错误处理演示工具

    展示概念：
    - 流式工具中的异常处理
    - 错误状态的通知

    Args:
        should_fail: 是否模拟失败
    """
    yield NotifyStreamEvent(data="🔧 开始执行可能失败的操作")

    try:
        yield NotifyStreamEvent(data="📋 检查输入参数...")
        await asyncio.sleep(0.3)

        if should_fail:
            yield NotifyStreamEvent(data="⚠️ 检测到错误条件", tag="warning")
            raise ValueError("模拟的业务逻辑错误")

        yield NotifyStreamEvent(data="✅ 参数检查通过", tag="success")
        yield NotifyStreamEvent(data="🚀 执行核心逻辑...")
        await asyncio.sleep(0.5)

        yield "操作成功完成，所有检查都通过了"

    except Exception as e:
        yield NotifyStreamEvent(data=f"❌ 操作失败: {str(e)}", tag="error")
        # 注意：即使出错，也要yield最终结果
        yield f"操作失败: {str(e)}"


# ============================================================================
# Agent配置
# ============================================================================

def create_basic_demo_agent():
    """创建基础演示Agent"""
    return Agent(
        name="BasicStreamingAgent",
        instructions="""你是一个@streaming_tool基础功能演示助手。你拥有以下工具：

1. simple_progress_tool - 演示基本的多步骤进度更新
2. countdown_tool - 演示实时倒计时和事件标签
3. error_demo_tool - 演示错误处理机制

根据用户请求选择合适的工具，并解释每个流式事件的含义。""",
        tools=[simple_progress_tool, countdown_tool, error_demo_tool],
    )


# ============================================================================
# 演示函数
# ============================================================================

async def demo_basic_concepts():
    """演示@streaming_tool的基础概念"""
    print("=" * 70)
    print("@streaming_tool 基础入门演示")
    print("核心概念：过程通知 vs 最终结果的严格分离")
    print("=" * 70)

    # 注意：这个演示直接调用工具，不需要配置真实的LLM模型
    print("\n说明：本演示直接调用流式工具，展示核心概念，无需配置LLM模型")

    from agents.run_context import RunContextWrapper
    ctx = RunContextWrapper(context=None)

    demos: list[DemoConfig] = [
        {
            "name": "基础进度更新",
            "tool": simple_progress_tool,
            "params": '{"task_name": "数据备份"}',
            "description": "演示最基本的多步骤进度通知"
        },
        {
            "name": "实时倒计时",
            "tool": countdown_tool,
            "params": '{"seconds": 3}',
            "description": "演示实时更新和事件标签的使用"
        },
        {
            "name": "错误处理（成功案例）",
            "tool": error_demo_tool,
            "params": '{"should_fail": false}',
            "description": "演示正常执行流程"
        },
        {
            "name": "错误处理（失败案例）",
            "tool": error_demo_tool,
            "params": '{"should_fail": true}',
            "description": "演示异常处理机制"
        }
    ]

    for i, demo in enumerate(demos, 1):
        print(f"\n{'-' * 50}")
        print(f"演示 {i}: {demo['name']}")
        print(f"说明: {demo['description']}")
        print(f"调用参数: {demo['params']}")
        print(f"{'-' * 50}")
        print("事件序列:")

        event_count = 0
        try:
            async for event in demo['tool'].on_invoke_tool(ctx, demo['params'], f"demo_{i}"):
                event_count += 1

                if isinstance(event, NotifyStreamEvent):
                    # 根据标签显示不同的图标
                    if event.tag == "success":
                        print(f"  [{event_count:2d}] ✅ {event.data}")
                    elif event.tag == "error":
                        print(f"  [{event_count:2d}] ❌ {event.data}")
                    elif event.tag == "warning":
                        print(f"  [{event_count:2d}] ⚠️ {event.data}")
                    elif event.tag == "countdown":
                        print(f"  [{event_count:2d}] ⏰ {event.data}")
                    elif event.tag == "complete":
                        print(f"  [{event_count:2d}] 🎉 {event.data}")
                    else:
                        print(f"  [{event_count:2d}] 📝 {event.data}")
                elif isinstance(event, str):
                    print(f"  [{event_count:2d}] 🎯 最终结果: {event}")
        except Exception as e:
            print(f"  [{event_count+1:2d}] ❌ 工具执行异常: {e}")

        print(f"📊 事件总数: {event_count}")


async def demo_direct_calls():
    """演示直接调用流式工具（不通过Agent）"""
    print("\n" + "=" * 70)
    print("直接调用演示：绕过Agent直接使用流式工具")
    print("适用场景：测试、调试或自定义集成")
    print("=" * 70)

    from agents.run_context import RunContextWrapper

    ctx = RunContextWrapper(context=None)

    print("\n直接调用 simple_progress_tool:")
    print("-" * 40)

    event_count = 0
    async for event in simple_progress_tool.on_invoke_tool(
        ctx,
        '{"task_name": "系统维护"}',
        "direct_call_demo"
    ):
        event_count += 1
        if isinstance(event, NotifyStreamEvent):
            tag_info = f" [标签: {event.tag}]" if event.tag else ""
            print(f"  [{event_count}] 事件: {event.data}{tag_info}")
        elif isinstance(event, str):
            print(f"  [{event_count}] 最终结果: {event}")

    print(f"\n📊 直接调用事件数: {event_count}")


async def demo_key_concepts():
    """演示关键概念总结"""
    print("\n" + "=" * 70)
    print("关键概念总结")
    print("=" * 70)

    concepts = [
        ("过程通知", "yield NotifyStreamEvent(data='...')", "不影响对话历史，纯展示用途"),
        ("最终结果", "yield '字符串结果'", "影响对话历史，必须是最后一个yield"),
        ("事件标签", "NotifyStreamEvent(tag='success')", "用于前端UI逻辑和事件分类"),
        ("增量输出", "NotifyStreamEvent(is_delta=True)", "用于打字机效果等流式文本"),
        ("终结信号", "yield '字符串' 后停止", "Runner会忽略后续的yield")
    ]

    print(f"{'概念':<12} {'代码示例':<35} {'说明'}")
    print("-" * 70)
    for concept, code, description in concepts:
        print(f"{concept:<12} {code:<35} {description}")

    print("\n🎯 核心原则:")
    print("  1. 严格分离'过程展示'与'最终结果'")
    print("  2. NotifyStreamEvent = 过程，字符串 = 结果")
    print("  3. 最后的yield必须是字符串")


if __name__ == "__main__":
    """运行基础演示套件"""
    async def main():
        await demo_basic_concepts()
        await demo_direct_calls()
        await demo_key_concepts()

        print("\n" + "=" * 70)
        print("🎉 基础演示完成！")
        print("📚 进阶内容请参考: examples/tools/streaming_tools.py")
        print("📖 完整文档请参考: docs/tools.md")
        print("=" * 70)

    asyncio.run(main())
