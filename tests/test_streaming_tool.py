"""
streaming_tool 功能的单元测试

测试 streaming_tool 装饰器、相关事件类型以及流式工具的行为是否符合预期。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator

import pytest

from agents import Agent, Runner, function_tool, streaming_tool
from agents.run_context import RunContextWrapper
from agents.stream_events import NotifyStreamEvent, StreamEvent, ToolStreamEndEvent, ToolStreamStartEvent
from agents.tool_context import ToolContext

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_text_message


class TestStreamingToolDecorator:
    """测试 streaming_tool 装饰器的基本功能"""

    def test_streaming_tool_creation(self):
        """测试 streaming_tool 装饰器能正确创建 StreamingTool 对象"""
        
        @streaming_tool
        async def simple_tool(message: str) -> AsyncGenerator[StreamEvent | str, Any]:
            """一个简单的流式工具"""
            yield NotifyStreamEvent(data=f"处理消息: {message}")
            yield f"完成处理: {message}"
        
        # 验证工具属性
        assert simple_tool.name == "simple_tool"
        assert simple_tool.description == "一个简单的流式工具"
        assert "message" in simple_tool.params_json_schema["properties"]
        assert simple_tool.params_json_schema["properties"]["message"]["type"] == "string"

    def test_streaming_tool_with_custom_name_and_description(self):
        """测试自定义名称和描述的 streaming_tool"""
        
        @streaming_tool(
            name_override="custom_tool",
            description_override="自定义描述"
        )
        async def original_name(data: str) -> AsyncGenerator[StreamEvent | str, Any]:
            yield f"处理: {data}"
        
        assert original_name.name == "custom_tool"
        assert original_name.description == "自定义描述"

    def test_streaming_tool_with_optional_parameters(self):
        """测试带有可选参数的 streaming_tool"""

        @streaming_tool
        async def tool_with_defaults(
            required_param: str,
            optional_param: int = 42,
            optional_str: str = "默认值"
        ) -> AsyncGenerator[StreamEvent | str, Any]:
            """带有可选参数的工具

            Args:
                required_param: 必需参数
                optional_param: 可选整数参数
                optional_str: 可选字符串参数
            """
            yield f"参数: {required_param}, {optional_param}, {optional_str}"

        schema = tool_with_defaults.params_json_schema
        assert "required_param" in schema["required"]
        # 注意：当前实现可能将所有参数都标记为必需，这是一个已知的行为
        # 我们主要验证默认值是否正确设置

        # 验证默认值
        assert schema["properties"]["optional_param"]["default"] == 42
        assert schema["properties"]["optional_str"]["default"] == "默认值"

    @pytest.mark.asyncio
    async def test_streaming_tool_execution_basic(self):
        """测试 streaming_tool 的基本执行"""

        @streaming_tool(enable_bracketing=False)
        async def basic_tool(name: str) -> AsyncGenerator[StreamEvent | str, Any]:
            yield NotifyStreamEvent(data=f"开始处理 {name}")
            await asyncio.sleep(0.001)  # 模拟异步操作
            yield NotifyStreamEvent(data="处理中...")
            yield f"完成处理 {name}"

        # 创建运行上下文
        ctx = RunContextWrapper(context=None)

        # 执行工具并收集事件
        events = []
        async for event in basic_tool.on_invoke_tool(ctx, '{"name": "测试"}', "test_call_123"):
            events.append(event)

        # 验证事件序列（没有括号事件）
        assert len(events) == 3

        # 第一个事件应该是 NotifyStreamEvent
        assert isinstance(events[0], NotifyStreamEvent)
        assert events[0].data == "开始处理 测试"
        assert events[0].tool_name == "basic_tool"
        assert events[0].tool_call_id == "test_call_123"

        # 第二个事件也是 NotifyStreamEvent
        assert isinstance(events[1], NotifyStreamEvent)
        assert events[1].data == "处理中..."

        # 最后一个事件应该是字符串（最终结果）
        assert isinstance(events[2], str)
        assert events[2] == "完成处理 测试"

    @pytest.mark.asyncio
    async def test_streaming_tool_with_bracketing(self):
        """测试启用括号事件的 streaming_tool"""

        @streaming_tool(enable_bracketing=True)
        async def bracketed_tool(data: str) -> AsyncGenerator[StreamEvent | str, Any]:
            yield NotifyStreamEvent(data=f"处理 {data}")
            yield f"结果: {data}"

        ctx = RunContextWrapper(context=None)

        events = []
        async for event in bracketed_tool.on_invoke_tool(ctx, '{"data": "测试数据"}', "bracket_test"):
            events.append(event)

        # 应该有开始事件、通知事件、结束事件、最终结果
        assert len(events) == 4

        # 第一个事件应该是 ToolStreamStartEvent
        assert isinstance(events[0], ToolStreamStartEvent)
        assert events[0].tool_name == "bracketed_tool"
        assert events[0].tool_call_id == "bracket_test"
        assert events[0].input_args == {"data": "测试数据"}

        # 第二个事件是通知
        assert isinstance(events[1], NotifyStreamEvent)
        assert events[1].data == "处理 测试数据"

        # 第三个事件应该是 ToolStreamEndEvent
        assert isinstance(events[2], ToolStreamEndEvent)
        assert events[2].tool_name == "bracketed_tool"
        assert events[2].tool_call_id == "bracket_test"

        # 最后是最终结果
        assert isinstance(events[3], str)
        assert events[3] == "结果: 测试数据"

    @pytest.mark.asyncio
    async def test_streaming_tool_error_handling_with_bracketing(self):
        """测试启用括号事件时的错误处理"""

        @streaming_tool(enable_bracketing=True)
        async def error_tool(should_fail: bool) -> AsyncGenerator[StreamEvent | str, Any]:
            yield NotifyStreamEvent(data="开始执行")
            if should_fail:
                raise ValueError("故意的错误")
            yield "成功完成"

        ctx = RunContextWrapper(context=None)

        # 测试正常情况
        events = []
        async for event in error_tool.on_invoke_tool(ctx, '{"should_fail": false}', "error_test"):
            events.append(event)

        assert len(events) == 4  # Start, Notify, End, Result
        assert isinstance(events[0], ToolStreamStartEvent)
        assert isinstance(events[1], NotifyStreamEvent)
        assert isinstance(events[2], ToolStreamEndEvent)
        assert isinstance(events[3], str)

        # 测试错误情况
        with pytest.raises(ValueError, match="故意的错误"):
            events = []
            async for event in error_tool.on_invoke_tool(ctx, '{"should_fail": true}', "error_test"):
                events.append(event)

        # 即使出错，也应该收到开始事件和通知事件
        assert len(events) >= 2
        assert isinstance(events[0], ToolStreamStartEvent)
        assert isinstance(events[1], NotifyStreamEvent)


class TestStreamingToolEvents:
    """测试 streaming_tool 相关的事件类型"""

    def test_notify_stream_event_creation(self):
        """测试 NotifyStreamEvent 的创建和属性"""
        
        # 基本创建
        event = NotifyStreamEvent(data="测试消息")
        assert event.data == "测试消息"
        assert event.is_delta is False
        assert event.tag is None
        assert event.tool_name is None
        assert event.tool_call_id is None
        assert event.type == "notify_stream_event"
        
        # 带所有参数的创建
        event_full = NotifyStreamEvent(
            data="增量消息",
            is_delta=True,
            tag="progress",
            tool_name="test_tool",
            tool_call_id="call_123"
        )
        assert event_full.data == "增量消息"
        assert event_full.is_delta is True
        assert event_full.tag == "progress"
        assert event_full.tool_name == "test_tool"
        assert event_full.tool_call_id == "call_123"

    def test_tool_stream_start_event_creation(self):
        """测试 ToolStreamStartEvent 的创建和属性"""
        
        input_args = {"param1": "value1", "param2": 42}
        event = ToolStreamStartEvent(
            tool_name="test_tool",
            tool_call_id="call_456",
            input_args=input_args
        )
        
        assert event.tool_name == "test_tool"
        assert event.tool_call_id == "call_456"
        assert event.input_args == input_args
        assert event.type == "tool_stream_start_event"

    def test_tool_stream_end_event_creation(self):
        """测试 ToolStreamEndEvent 的创建和属性"""
        
        event = ToolStreamEndEvent(
            tool_name="test_tool",
            tool_call_id="call_789"
        )
        
        assert event.tool_name == "test_tool"
        assert event.tool_call_id == "call_789"
        assert event.type == "tool_stream_end_event"


class TestStreamingToolIntegration:
    """测试 streaming_tool 与 Agent 和 Runner 的集成"""

    @pytest.mark.asyncio
    async def test_streaming_tool_in_agent(self):
        """测试在 Agent 中使用 streaming_tool"""

        @streaming_tool
        async def progress_tool(task: str) -> AsyncGenerator[StreamEvent | str, Any]:
            """执行任务并报告进度"""
            yield NotifyStreamEvent(data=f"开始执行任务: {task}")
            await asyncio.sleep(0.001)
            yield NotifyStreamEvent(data="任务进行中...")
            await asyncio.sleep(0.001)
            yield NotifyStreamEvent(data="即将完成...")
            yield f"任务 '{task}' 已完成"

        # 创建模拟模型
        model = FakeModel()
        model.set_next_output([
            get_function_tool_call("progress_tool", json.dumps({"task": "数据处理"})),
            get_text_message("任务执行完毕")
        ])

        # 创建 Agent
        agent = Agent(
            name="测试代理",
            model=model,
            tools=[progress_tool]
        )

        # 运行并收集事件
        result = Runner.run_streamed(agent, input="请执行数据处理任务")

        notify_events = []
        tool_start_events = []
        tool_end_events = []

        async for event in result.stream_events():
            if isinstance(event, NotifyStreamEvent):
                notify_events.append(event)
            elif isinstance(event, ToolStreamStartEvent):
                tool_start_events.append(event)
            elif isinstance(event, ToolStreamEndEvent):
                tool_end_events.append(event)

        # 验证收到了预期的通知事件
        assert len(notify_events) >= 3
        assert any("开始执行任务: 数据处理" in event.data for event in notify_events)
        assert any("任务进行中..." in event.data for event in notify_events)
        assert any("即将完成..." in event.data for event in notify_events)

        # 验证工具名称和调用ID被正确设置
        for event in notify_events:
            assert event.tool_name == "progress_tool"
            assert event.tool_call_id is not None

    @pytest.mark.asyncio
    async def test_streaming_tool_with_delta_events(self):
        """测试使用增量事件的 streaming_tool"""

        @streaming_tool(enable_bracketing=False)
        async def typewriter_tool(text: str) -> AsyncGenerator[StreamEvent | str, Any]:
            """模拟打字机效果的工具"""
            yield NotifyStreamEvent(data="开始输出...")

            full_text = ""
            for char in text:
                full_text += char
                yield NotifyStreamEvent(data=char, is_delta=True)
                await asyncio.sleep(0.001)

            yield f"完整文本: {full_text}"

        ctx = RunContextWrapper(context=None)

        events = []
        async for event in typewriter_tool.on_invoke_tool(ctx, '{"text": "Hello"}', "typewriter_test"):
            events.append(event)

        # 应该有: 开始消息 + 5个字符的增量事件 + 最终结果
        assert len(events) == 7

        # 第一个是开始消息
        assert isinstance(events[0], NotifyStreamEvent)
        assert events[0].data == "开始输出..."
        assert events[0].is_delta is False

        # 接下来5个是增量事件
        expected_chars = ["H", "e", "l", "l", "o"]
        for i, char in enumerate(expected_chars):
            event = events[i + 1]
            assert isinstance(event, NotifyStreamEvent)
            assert event.data == char
            assert event.is_delta is True

        # 最后是最终结果
        assert isinstance(events[-1], str)
        assert events[-1] == "完整文本: Hello"

    @pytest.mark.asyncio
    async def test_streaming_tool_with_context_injection(self):
        """测试 streaming_tool 的上下文注入功能"""

        @streaming_tool(enable_bracketing=False)
        async def context_tool(
            ctx: RunContextWrapper,
            message: str
        ) -> AsyncGenerator[StreamEvent | str, Any]:
            """需要上下文的流式工具（上下文参数必须在第一位）"""
            is_context_valid = isinstance(ctx, RunContextWrapper)
            yield NotifyStreamEvent(data=f"上下文有效: {is_context_valid}")
            yield f"消息: {message}, 上下文: {type(ctx).__name__}"

        # 创建模拟的运行上下文
        ctx = RunContextWrapper(context=None)

        events = []
        async for event in context_tool.on_invoke_tool(ctx, '{"message": "测试消息"}', "context_test"):
            events.append(event)

        assert len(events) == 2

        # 验证上下文注入成功
        assert isinstance(events[0], NotifyStreamEvent)
        assert "上下文有效: True" in events[0].data

        assert isinstance(events[1], str)
        assert "RunContextWrapper" in events[1]

    @pytest.mark.asyncio
    async def test_streaming_tool_with_tags(self):
        """测试带有标签的 NotifyStreamEvent"""

        @streaming_tool(enable_bracketing=False)
        async def tagged_tool(operation: str) -> AsyncGenerator[StreamEvent | str, Any]:
            """使用不同标签的工具"""
            yield NotifyStreamEvent(data="开始操作", tag="start")
            yield NotifyStreamEvent(data="进度: 50%", tag="progress")
            yield NotifyStreamEvent(data="操作成功", tag="success")
            yield f"完成操作: {operation}"

        ctx = RunContextWrapper(context=None)

        events = []
        async for event in tagged_tool.on_invoke_tool(ctx, '{"operation": "测试操作"}', "tagged_test"):
            events.append(event)

        assert len(events) == 4

        # 验证标签
        notify_events = [e for e in events if isinstance(e, NotifyStreamEvent)]
        assert len(notify_events) == 3

        assert notify_events[0].tag == "start"
        assert notify_events[1].tag == "progress"
        assert notify_events[2].tag == "success"

    @pytest.mark.asyncio
    async def test_multiple_streaming_tools_in_agent(self):
        """测试在一个 Agent 中使用多个 streaming_tool"""

        @streaming_tool
        async def tool_a(data: str) -> AsyncGenerator[StreamEvent | str, Any]:
            yield NotifyStreamEvent(data=f"工具A处理: {data}")
            yield f"工具A结果: {data}"

        @streaming_tool
        async def tool_b(data: str) -> AsyncGenerator[StreamEvent | str, Any]:
            yield NotifyStreamEvent(data=f"工具B处理: {data}")
            yield f"工具B结果: {data}"

        # 创建模拟模型，模拟调用两个工具
        model = FakeModel()
        model.add_multiple_turn_outputs([
            [get_function_tool_call("tool_a", json.dumps({"data": "测试A"}))],
            [get_function_tool_call("tool_b", json.dumps({"data": "测试B"}))],
            [get_text_message("所有工具执行完毕")]
        ])

        # 创建 Agent
        agent = Agent(
            name="多工具代理",
            model=model,
            tools=[tool_a, tool_b]
        )

        # 运行并收集事件
        result = Runner.run_streamed(agent, input="请依次执行工具A和工具B")

        notify_events = []
        async for event in result.stream_events():
            if isinstance(event, NotifyStreamEvent):
                notify_events.append(event)

        # 应该收到来自两个工具的通知事件
        assert len(notify_events) >= 2

        tool_a_events = [e for e in notify_events if e.tool_name == "tool_a"]
        tool_b_events = [e for e in notify_events if e.tool_name == "tool_b"]

        assert len(tool_a_events) >= 1
        assert len(tool_b_events) >= 1

        assert any("工具A处理: 测试A" in e.data for e in tool_a_events)
        assert any("工具B处理: 测试B" in e.data for e in tool_b_events)


class TestStreamingToolAdvanced:
    """测试 streaming_tool 的高级功能"""

    @pytest.mark.asyncio
    async def test_streaming_tool_agent_as_tool(self):
        """测试 Agent.as_tool(streaming=True) 功能"""

        # 创建一个简单的子 Agent
        @streaming_tool
        async def sub_tool(task: str) -> AsyncGenerator[StreamEvent | str, Any]:
            yield NotifyStreamEvent(data=f"子工具开始处理: {task}")
            yield f"子工具完成: {task}"

        sub_model = FakeModel()
        sub_model.set_next_output([
            get_function_tool_call("sub_tool", json.dumps({"task": "子任务"})),
            get_text_message("子Agent完成")
        ])

        sub_agent = Agent(
            name="子代理",
            model=sub_model,
            tools=[sub_tool]
        )

        # 创建主 Agent，使用子 Agent 作为流式工具
        main_model = FakeModel()
        main_model.set_next_output([
            get_function_tool_call("run_sub_agent", json.dumps({"input": "执行子任务"})),
            get_text_message("主Agent完成")
        ])

        main_agent = Agent(
            name="主代理",
            model=main_model,
            tools=[
                sub_agent.as_tool(
                    tool_name="run_sub_agent",
                    tool_description="运行子代理",
                    streaming=True
                )
            ]
        )

        # 运行并收集事件
        result = Runner.run_streamed(main_agent, input="请运行子代理")

        notify_events = []
        async for event in result.stream_events():
            if isinstance(event, NotifyStreamEvent):
                notify_events.append(event)

        # 应该收到来自子工具的通知事件
        assert len(notify_events) >= 1
        assert any("子工具开始处理" in event.data for event in notify_events)

    @pytest.mark.asyncio
    async def test_streaming_tool_error_handling(self):
        """测试 streaming_tool 的错误处理"""

        @streaming_tool(enable_bracketing=False)
        async def error_prone_tool(should_fail: str) -> AsyncGenerator[StreamEvent | str, Any]:
            yield NotifyStreamEvent(data="开始执行可能失败的操作")

            if should_fail.lower() == "true":
                raise RuntimeError("工具执行失败")

            yield NotifyStreamEvent(data="操作成功")
            yield "操作完成"

        # 测试直接调用工具的错误处理
        ctx = RunContextWrapper(context=None)

        # 测试成功情况
        events = []
        async for event in error_prone_tool.on_invoke_tool(ctx, '{"should_fail": "false"}', "error_test"):
            events.append(event)

        assert len(events) == 3
        assert isinstance(events[0], NotifyStreamEvent)
        assert "开始执行可能失败的操作" in events[0].data
        assert isinstance(events[1], NotifyStreamEvent)
        assert "操作成功" in events[1].data
        assert isinstance(events[2], str)
        assert "操作完成" in events[2]

        # 测试失败情况
        with pytest.raises(RuntimeError, match="工具执行失败"):
            events = []
            async for event in error_prone_tool.on_invoke_tool(ctx, '{"should_fail": "true"}', "error_test"):
                events.append(event)

        # 即使出错，也应该收到开始执行的通知
        assert len(events) >= 1
        assert isinstance(events[0], NotifyStreamEvent)
        assert "开始执行可能失败的操作" in events[0].data

    @pytest.mark.asyncio
    async def test_streaming_tool_parameter_validation(self):
        """测试 streaming_tool 的参数验证"""

        @streaming_tool(enable_bracketing=False)
        async def validated_tool(
            required_str: str,
            required_int: int,
            optional_bool: bool = False
        ) -> AsyncGenerator[StreamEvent | str, Any]:
            """需要参数验证的工具"""
            yield NotifyStreamEvent(data=f"参数: {required_str}, {required_int}, {optional_bool}")
            yield f"验证通过: {required_str}-{required_int}-{optional_bool}"

        ctx = RunContextWrapper(context=None)

        # 测试有效参数
        events = []
        async for event in validated_tool.on_invoke_tool(
            ctx,
            '{"required_str": "测试", "required_int": 42, "optional_bool": true}',
            "validation_test"
        ):
            events.append(event)

        assert len(events) == 2
        assert isinstance(events[0], NotifyStreamEvent)
        assert "参数: 测试, 42, True" in events[0].data
        assert isinstance(events[1], str)
        assert "验证通过: 测试-42-True" in events[1]

        # 测试缺少必需参数的情况
        with pytest.raises(Exception):  # 应该抛出参数验证错误
            events = []
            async for event in validated_tool.on_invoke_tool(ctx, '{"required_str": "测试"}', "validation_test"):
                events.append(event)

    @pytest.mark.asyncio
    async def test_streaming_tool_concurrent_execution(self):
        """测试 streaming_tool 的并发执行"""

        @streaming_tool(enable_bracketing=False)
        async def concurrent_tool(delay: float, name: str) -> AsyncGenerator[StreamEvent | str, Any]:
            """支持并发的工具"""
            yield NotifyStreamEvent(data=f"{name} 开始执行")
            await asyncio.sleep(delay)
            yield NotifyStreamEvent(data=f"{name} 执行中...")
            await asyncio.sleep(delay)
            yield f"{name} 完成"

        ctx1 = RunContextWrapper(context=None)
        ctx2 = RunContextWrapper(context=None)

        # 并发执行两个工具实例
        async def run_tool_1():
            events = []
            async for event in concurrent_tool.on_invoke_tool(
                ctx1,
                '{"delay": 0.001, "name": "工具1"}',
                "concurrent_1"
            ):
                events.append(event)
            return events

        async def run_tool_2():
            events = []
            async for event in concurrent_tool.on_invoke_tool(
                ctx2,
                '{"delay": 0.001, "name": "工具2"}',
                "concurrent_2"
            ):
                events.append(event)
            return events

        # 并发运行
        results = await asyncio.gather(run_tool_1(), run_tool_2())

        events1, events2 = results

        # 验证两个工具都正确执行
        assert len(events1) == 3
        assert len(events2) == 3

        # 验证工具1的事件
        assert any("工具1 开始执行" in str(e.data) for e in events1 if isinstance(e, NotifyStreamEvent))
        assert any("工具1 完成" in str(e) for e in events1 if isinstance(e, str))

        # 验证工具2的事件
        assert any("工具2 开始执行" in str(e.data) for e in events2 if isinstance(e, NotifyStreamEvent))
        assert any("工具2 完成" in str(e) for e in events2 if isinstance(e, str))

    def test_streaming_tool_vs_function_tool_api_consistency(self):
        """测试 streaming_tool 与 function_tool 的 API 一致性"""

        # 创建一个 function_tool
        @function_tool
        def regular_tool(message: str, count: int = 1) -> str:
            """常规工具"""
            return f"处理了 {count} 次: {message}"

        # 创建一个对应的 streaming_tool
        @streaming_tool
        async def streaming_version(message: str, count: int = 1) -> AsyncGenerator[StreamEvent | str, Any]:
            """流式版本的工具"""
            yield NotifyStreamEvent(data=f"开始处理 {count} 次")
            yield f"处理了 {count} 次: {message}"

        # 验证两个工具的基本属性一致性
        assert regular_tool.name == "regular_tool"
        assert streaming_version.name == "streaming_version"

        # 验证参数 schema 结构相似
        regular_schema = regular_tool.params_json_schema
        streaming_schema = streaming_version.params_json_schema

        # 两者都应该有相同的参数
        assert set(regular_schema["properties"].keys()) == set(streaming_schema["properties"].keys())
        assert regular_schema["required"] == streaming_schema["required"]

        # 验证参数类型一致
        for param_name in regular_schema["properties"]:
            regular_param = regular_schema["properties"][param_name]
            streaming_param = streaming_schema["properties"][param_name]
            assert regular_param["type"] == streaming_param["type"]

            # 检查默认值
            if "default" in regular_param:
                assert "default" in streaming_param
                assert regular_param["default"] == streaming_param["default"]
