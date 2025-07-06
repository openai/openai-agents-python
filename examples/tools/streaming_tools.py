"""
@streaming_tool 权威使用示例集

本文件展示了 @streaming_tool 装饰器的各种核心使用场景，包括：
1. 多阶段进度更新
2. 打字机效果（增量输出）
3. 高级流程编排（Agent as Tool）
4. 错误处理与括号事件

设计哲学：严格分离"过程展示"与"最终结果"
- 过程展示：yield NotifyStreamEvent(...) - 不影响对话历史
- 最终结果：yield "字符串结果" - 作为最后一个yield，影响对话历史
"""
import asyncio
from collections.abc import AsyncGenerator
from typing import Any, Union

from agents import Agent, NotifyStreamEvent, Runner, StreamEvent, streaming_tool

# ============================================================================
# 场景一：多阶段进度更新
# 核心模式：yield NotifyStreamEvent(...) 用于过程展示
# ============================================================================

@streaming_tool
async def data_pipeline_tool(source_url: str, batch_size: int = 100) -> AsyncGenerator[StreamEvent | str, Any]:
    """数据管道处理工具 - 演示多阶段进度更新

    这个工具展示了如何在长时间运行的任务中提供详细的进度反馈。
    每个 NotifyStreamEvent 都是纯展示性质，不会影响对话历史。

    Args:
        source_url: 数据源URL
        batch_size: 批处理大小
    """
    # 阶段1：连接
    yield NotifyStreamEvent(data=f"[1/4] 正在连接到数据源: {source_url}")
    await asyncio.sleep(0.5)  # 模拟网络延迟

    # 阶段2：下载（带成功标签）
    yield NotifyStreamEvent(data="[2/4] ✅ 连接成功，开始下载数据...", tag="success")
    await asyncio.sleep(0.3)

    # 阶段3：批处理进度（演示动态进度更新）
    total_records = 1234
    processed = 0

    while processed < total_records:
        batch_end = min(processed + batch_size, total_records)
        yield NotifyStreamEvent(
            data=f"[3/4] 处理记录 {processed + 1}-{batch_end}/{total_records}",
            tag="progress"
        )
        processed = batch_end
        await asyncio.sleep(0.1)

    # 阶段4：最终处理
    yield NotifyStreamEvent(data="[4/4] 数据验证和清理中...", tag="processing")
    await asyncio.sleep(0.2)

    yield NotifyStreamEvent(data="🎉 处理完成!", tag="success")

    # 关键：最终结果必须是字符串，且作为最后一个yield
    yield f"数据管道处理成功！从 {source_url} 处理了 {total_records} 条记录，批大小: {batch_size}"


# ============================================================================
# 场景二：RAG打字机效果（增量输出）
# 核心模式：is_delta=True 用于流式文本输出
# ============================================================================

@streaming_tool
async def research_and_summarize_tool(topic: str) -> AsyncGenerator[StreamEvent | str, Any]:
    """RAG研究总结工具 - 演示打字机效果的实际应用

    这个工具模拟了一个真实的RAG场景：检索文档 -> 生成总结 -> 流式输出
    展示了如何结合进度通知和增量文本输出。

    Args:
        topic: 研究主题
    """
    # 第一阶段：检索
    yield NotifyStreamEvent(data=f"🔍 正在检索关于'{topic}'的文档...")
    await asyncio.sleep(0.8)  # 模拟检索时间

    # 模拟检索结果
    documents_found = 15
    yield NotifyStreamEvent(data=f"✅ 检索完成，找到 {documents_found} 个相关文档", tag="success")

    # 第二阶段：分析
    yield NotifyStreamEvent(data="🧠 正在分析文档内容，生成总结...")
    await asyncio.sleep(0.5)

    # 第三阶段：流式输出总结（打字机效果）
    yield NotifyStreamEvent(data="📝 开始输出总结：", tag="output_start")

    # 模拟LLM流式输出
    summary_parts = [
        f"关于{topic}的研究总结：\n\n",
        "1. 核心概念：",
        f"{topic}是一个重要的技术领域，",
        "具有广泛的应用前景。\n\n",
        "2. 主要特点：\n",
        "- 高效性能\n",
        "- 易于扩展\n",
        "- 社区活跃\n\n",
        "3. 应用场景：\n",
        "在多个行业中都有成功案例，",
        "特别是在数据处理和自动化领域。\n\n",
        "总结完成。"
    ]

    full_summary = ""
    for part in summary_parts:
        full_summary += part
        # 关键：使用 is_delta=True 实现打字机效果
        yield NotifyStreamEvent(data=part, is_delta=True, tag="typewriter")
        await asyncio.sleep(0.1)  # 控制打字速度

    yield NotifyStreamEvent(data="✅ 总结生成完成", tag="complete")

    # 最终结果：完整的总结文本
    yield full_summary


@streaming_tool
async def simple_typewriter_tool(text: str, speed: float = 0.05) -> AsyncGenerator[StreamEvent | str, Any]:
    """简单打字机工具 - 基础的字符级增量输出

    Args:
        text: 要显示的文本
        speed: 打字速度（秒/字符）
    """
    yield NotifyStreamEvent(data="开始打字机输出...", tag="start")

    full_text = ""
    for char in text:
        full_text += char
        # 使用 is_delta=True 表示这是增量更新
        yield NotifyStreamEvent(data=char, is_delta=True, tag="typewriter")
        await asyncio.sleep(speed)

    yield NotifyStreamEvent(data="\n✅ 输出完成", tag="complete")
    yield f"打字机输出完成: '{full_text}'"


# ============================================================================
# 场景三：高级流程编排 - Agent as Tool
# 核心模式：Agent.as_tool(streaming=True) 实现无缝嵌套
# ============================================================================

# 首先定义一个专门的文件分析子Agent
def create_file_analysis_agent():
    """创建专门的文件分析Agent"""

    @streaming_tool
    async def analyze_content_tool(content_type: str) -> AsyncGenerator[StreamEvent | str, Any]:
        """内容分析工具"""
        yield NotifyStreamEvent(data=f"🔍 开始{content_type}分析...")
        await asyncio.sleep(0.3)

        analysis_steps = [
            "词汇统计", "语法检查", "关键词提取", "情感分析"
        ]

        for i, step in enumerate(analysis_steps, 1):
            yield NotifyStreamEvent(data=f"[{i}/{len(analysis_steps)}] {step}中...", tag="progress")
            await asyncio.sleep(0.2)

        yield f"{content_type}分析完成：发现 1,234 个词汇，情感倾向为积极"

    return Agent(
        name="FileAnalysisAgent",
        instructions="你是一个专业的文件分析专家。使用analyze_content_tool来分析不同类型的内容。",
        tools=[analyze_content_tool]
    )


# 创建使用子Agent的主编排Agent
def create_orchestrator_agent():
    """创建编排Agent，演示Agent as Tool的强大功能"""

    # 创建专门的子Agent
    file_analysis_agent = create_file_analysis_agent()

    # 将子Agent包装成流式工具
    return Agent(
        name="DocumentProcessorAgent",
        instructions="""你是一个文档处理编排器。当用户要求分析文件时：
1. 使用 run_file_analysis 工具调用专门的分析Agent
2. 解释分析结果并提供建议""",
        tools=[
            file_analysis_agent.as_tool(
                tool_name="run_file_analysis",
                tool_description="运行专门的文件分析Agent",
                streaming=True,  # 关键：启用流式输出
                enable_bracketing=True  # 推荐：提供清晰的嵌套层次
            )
        ]
    )


# 传统的单一工具实现（用于对比）
@streaming_tool
async def traditional_file_analyzer(file_path: str) -> AsyncGenerator[StreamEvent | str, Any]:
    """传统的文件分析工具 - 用于与Agent as Tool方案对比

    Args:
        file_path: 要分析的文件路径
    """
    yield NotifyStreamEvent(data=f"开始分析文件: {file_path}")

    # 模拟文件读取
    yield NotifyStreamEvent(data="📖 读取文件内容...", tag="reading")
    await asyncio.sleep(0.3)

    # 模拟各种分析步骤
    analysis_steps = [
        ("🔍 词汇分析", "analyzing"),
        ("📊 统计计算", "calculating"),
        ("🎯 关键词提取", "extracting"),
        ("📈 生成报告", "reporting")
    ]

    results: dict[str, Union[int, float, list[str]]] = {}
    for step_name, tag in analysis_steps:
        yield NotifyStreamEvent(data=step_name, tag=tag)
        await asyncio.sleep(0.4)

        # 模拟分析结果
        if "词汇" in step_name:
            results["word_count"] = 1542
        elif "统计" in step_name:
            results["avg_sentence_length"] = 12.3
        elif "关键词" in step_name:
            results["keywords"] = ["Python", "AI", "工具", "流式"]

    yield NotifyStreamEvent(data="✅ 分析完成!", tag="success")

    # 返回分析结果
    keywords = results['keywords']
    assert isinstance(keywords, list), "keywords should be a list"
    yield f"""文件分析完成: {file_path}
📊 统计结果:
- 词汇数量: {results['word_count']}
- 平均句长: {results['avg_sentence_length']}
- 关键词: {', '.join(keywords)}"""


# ============================================================================
# 场景四：括号事件演示 - 清晰的流程边界
# 核心模式：enable_bracketing=True 提供嵌套上下文
# ============================================================================

@streaming_tool(enable_bracketing=True)
async def complex_workflow_tool(task_name: str) -> AsyncGenerator[StreamEvent | str, Any]:
    """复杂工作流工具 - 演示括号事件的重要性

    enable_bracketing=True 会自动生成 ToolStreamStartEvent 和 ToolStreamEndEvent，
    为客户端提供清晰的流程边界，特别适用于嵌套调用场景。

    Args:
        task_name: 任务名称
    """
    yield NotifyStreamEvent(data=f"🚀 启动复杂工作流: {task_name}")

    # 模拟多个子任务
    subtasks = ["环境初始化", "数据收集", "核心处理", "结果验证", "清理工作"]

    for i, subtask in enumerate(subtasks, 1):
        yield NotifyStreamEvent(
            data=f"[{i}/{len(subtasks)}] {subtask}中...",
            tag="workflow"
        )
        await asyncio.sleep(0.3)

        if subtask == "核心处理":
            # 在核心处理步骤中添加更详细的进度
            for j in range(1, 4):
                yield NotifyStreamEvent(
                    data=f"  └─ 处理阶段 {j}/3: 正在优化算法参数",
                    tag="subprocess"
                )
                await asyncio.sleep(0.2)

    yield NotifyStreamEvent(data="✅ 所有步骤完成!", tag="success")

    # 注意：ToolStreamEndEvent 会在这个 yield 之前自动发送
    yield f"工作流 '{task_name}' 执行完成！所有 {len(subtasks)} 个步骤已成功完成，耗时约 {len(subtasks) * 0.3:.1f} 秒。"


# ============================================================================
# 演示用的Agent配置
# ============================================================================

def create_demo_agent():
    """创建演示用的Agent，集成所有流式工具"""
    return Agent(
        name="StreamingToolDemoAgent",
        instructions="""你是一个@streaming_tool功能演示专家。你拥有以下能力：

1. data_pipeline_tool - 演示多阶段进度更新
2. research_and_summarize_tool - 演示RAG场景的打字机效果
3. simple_typewriter_tool - 演示基础的字符级增量输出
4. traditional_file_analyzer - 演示传统的单一工具方案
5. complex_workflow_tool - 演示括号事件的重要性

当用户请求演示时，选择最合适的工具并详细解释每个事件的含义。""",
        tools=[
            data_pipeline_tool,
            research_and_summarize_tool,
            simple_typewriter_tool,
            traditional_file_analyzer,
            complex_workflow_tool
        ],
    )


# ============================================================================
# 核心演示函数
# ============================================================================

async def demo_core_scenarios():
    """演示@streaming_tool的核心使用场景"""
    print("=" * 80)
    print("@streaming_tool 权威使用演示")
    print("设计哲学：严格分离'过程展示'与'最终结果'")
    print("=" * 80)

    demo_agent = create_demo_agent()

    scenarios = [
        {
            "name": "场景一：多阶段进度更新",
            "input": "请处理来自 https://api.example.com/data 的数据，批大小设为50",
            "description": "演示如何在长时间任务中提供详细的阶段性进度反馈"
        },
        {
            "name": "场景二：RAG打字机效果",
            "input": "请研究并总结'人工智能'这个主题",
            "description": "演示检索增强生成(RAG)场景中的流式文本输出"
        },
        {
            "name": "场景三：括号事件演示",
            "input": "执行名为'机器学习模型训练'的复杂工作流",
            "description": "演示enable_bracketing=True如何提供清晰的流程边界"
        }
    ]

    for i, scenario in enumerate(scenarios, 1):
        print(f"\n{'-' * 60}")
        print(f"{scenario['name']}")
        print(f"说明: {scenario['description']}")
        print(f"用户输入: {scenario['input']}")
        print(f"{'-' * 60}")
        print("流式事件序列:")

        result = Runner.run_streamed(demo_agent, input=scenario['input'])

        event_count = 0
        async for event in result.stream_events():
            event_count += 1

            if event.type == "notify_stream_event":
                # 根据标签和类型显示不同格式
                if event.tag == "success":
                    print(f"  [{event_count:2d}] ✅ {event.data}")
                elif event.tag == "error":
                    print(f"  [{event_count:2d}] ❌ {event.data}")
                elif event.tag == "progress":
                    print(f"  [{event_count:2d}] 📊 {event.data}")
                elif event.tag == "typewriter" and event.is_delta:
                    print(event.data, end="", flush=True)
                elif event.tag == "workflow":
                    print(f"  [{event_count:2d}] 🔄 {event.data}")
                else:
                    print(f"  [{event_count:2d}] 📝 {event.data}")
            elif event.type == "tool_stream_start_event":
                print(f"  [{event_count:2d}] 🚀 [开始] {event.tool_name}")
            elif event.type == "tool_stream_end_event":
                print(f"  [{event_count:2d}] 🏁 [结束] {event.tool_name}")

        print(f"\n💡 最终结果: {result.final_output}")
        print(f"📊 总事件数: {event_count}")

        if i < len(scenarios):
            print("\n" + "=" * 80)


async def demo_agent_as_tool():
    """演示Agent as Tool的强大功能"""
    print("\n" + "=" * 80)
    print("高级场景：Agent as Tool - 无缝流程编排")
    print("核心优势：Agent.as_tool(streaming=True) 实现零代码嵌套")
    print("=" * 80)

    # 创建编排Agent
    orchestrator = create_orchestrator_agent()

    print("用户输入: 请分析一个Python代码文件的内容")
    print("-" * 60)
    print("嵌套事件流（注意层次结构）:")

    result = Runner.run_streamed(orchestrator, input="请分析一个Python代码文件的内容")

    event_count = 0
    indent_level = 0

    async for event in result.stream_events():
        event_count += 1
        indent = "  " * indent_level

        if event.type == "tool_stream_start_event":
            print(f"{indent}[{event_count:2d}] 🚀 开始调用: {event.tool_name}")
            if event.tool_name == "run_file_analysis":
                indent_level += 1
        elif event.type == "tool_stream_end_event":
            if hasattr(event, 'tool_name') and event.tool_name == "run_file_analysis":
                indent_level = max(0, indent_level - 1)
            print(f"{indent}[{event_count:2d}] 🏁 结束调用: {event.tool_name}")
        elif event.type == "notify_stream_event":
            if event.tag == "progress":
                print(f"{indent}[{event_count:2d}] 📊 {event.data}")
            else:
                print(f"{indent}[{event_count:2d}] 📝 {event.data}")

    print(f"\n💡 编排结果: {result.final_output}")
    print(f"📊 总事件数: {event_count}")
    print("\n🎯 关键观察：子Agent的所有事件都被自动转发到主流中！")


async def demo_direct_tool_calls():
    """演示直接调用流式工具（不通过Agent）"""
    print("\n" + "=" * 80)
    print("底层演示：直接调用流式工具")
    print("适用场景：测试、调试或自定义集成")
    print("=" * 80)

    from agents.run_context import RunContextWrapper

    ctx = RunContextWrapper(context=None)

    print("直接调用 data_pipeline_tool:")
    print("-" * 40)

    event_count = 0
    async for event in data_pipeline_tool.on_invoke_tool(
        ctx,
        '{"source_url": "https://example.com/api", "batch_size": 50}',
        "direct_demo_call"
    ):
        event_count += 1
        if isinstance(event, NotifyStreamEvent):
            tag_info = f" (标签: {event.tag})" if event.tag else ""
            print(f"  [{event_count}] 事件: {event.data}{tag_info}")
        elif isinstance(event, str):
            print(f"  [{event_count}] 最终结果: {event}")

    print(f"\n📊 直接调用事件数: {event_count}")


async def demo_quick_reference():
    """快速参考：开发者意图与代码映射"""
    print("\n" + "=" * 80)
    print("快速参考：@streaming_tool 开发者意图映射表")
    print("=" * 80)

    reference_table = [
        ("报告完整的进度步骤", "yield NotifyStreamEvent(data='...')", "否"),
        ("流式输出文本(打字机)", "yield NotifyStreamEvent(data='...', is_delta=True)", "否"),
        ("标记特殊事件类型", "yield NotifyStreamEvent(data='...', tag='success')", "否"),
        ("提供工具最终结果", "yield '最终的字符串结果' (作为最后一个yield)", "是"),
        ("启用流程括号事件", "@streaming_tool(enable_bracketing=True)", "否"),
        ("Agent作为流式工具", "agent.as_tool(streaming=True)", "是(由子流程决定)")
    ]

    print(f"{'开发者意图':<20} {'应编写的代码':<45} {'影响对话历史?'}")
    print("-" * 80)
    for intent, code, affects_history in reference_table:
        print(f"{intent:<20} {code:<45} {affects_history}")

    print("\n🔑 核心原则:")
    print("  1. 所有 NotifyStreamEvent 都是纯展示性质，不影响对话历史")
    print("  2. 只有最后的 yield '字符串' 会被记录为工具输出")
    print("  3. yield '字符串' 是终结信号，之后的yield会被忽略")


if __name__ == "__main__":
    """运行完整的@streaming_tool演示套件"""
    async def main():
        await demo_core_scenarios()
        await demo_agent_as_tool()
        await demo_direct_tool_calls()
        await demo_quick_reference()

        print("\n" + "=" * 80)
        print("🎉 @streaming_tool 演示完成！")
        print("📚 更多信息请参考: docs/tools.md")
        print("=" * 80)

    asyncio.run(main())
