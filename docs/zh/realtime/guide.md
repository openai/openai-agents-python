---
search:
  exclude: true
---
# 指南

本指南深入介绍如何使用 OpenAI Agents SDK 的实时功能构建支持语音的 AI 智能体。

!!! warning "测试版功能"
实时智能体处于测试版。随着我们改进实现，可能会出现重大更改。

## 概述

实时智能体支持对话流程，实时处理音频和文本输入，并以实时音频进行响应。它们与 OpenAI 的实时 API 保持持久连接，实现低延迟的自然语音对话，并能优雅地处理打断情况。

## 架构

### 核心组件

实时系统由以下关键组件组成：

- **RealtimeAgent**: 由指令、工具、交接组成的智能体。
- **RealtimeRunner**: 管理配置。你可以调用 `runner.run()` 来获取会话。
- **RealtimeSession**: 单个交互会话。通常，每次用户开始对话时创建一个，并在对话结束时保持活动状态。
- **RealtimeModel**: 底层模型接口（通常是 OpenAI 的 WebSocket 实现）

### 会话流程

典型的实时会话流程如下：

1. 使用指令、工具、交接**创建 RealtimeAgent**。
2. 使用智能体和配置选项**设置 RealtimeRunner**。
3. 使用 `await runner.run()`**开始会话**，返回 RealtimeSession。
4. 使用 `send_audio()` 或 `send_message()`**发送音频或文本消息**。
5. 通过迭代会话**监听事件**。事件包括音频输出、转录、工具调用、交接、错误。
6. **处理打断**，当用户在智能体说话时覆盖说话，这将自动停止当前音频生成。

会话维护对话历史并管理与实时模型的持久连接。

## 智能体配置

RealtimeAgent 的工作方式与常规的 Agent 类相似，但有一些关键区别。有关完整的 API 详情，请参阅 [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] API 参考。

与常规智能体的主要区别:

-   模型选择在会话级别配置，而不是智能体级别。
-   不支持结构化输出（不支持 `outputType`）。
-   可以为每个智能体配置语音，但在第一个智能体发言后无法更改。
-   所有其他功能如工具、交接和指令的工作方式相同。

## 会话配置

### 模型设置

会话配置允许您控制底层实时模型的行为。您可以配置模型名称（如 `gpt-realtime`）、语音选择（alloy、echo、fable、onyx、nova、shimmer）以及支持的模态（文本和/或音频）。输入和输出的音频格式都可以设置，PCM16 是默认格式。

### 音频配置

音频设置控制会话如何处理语音输入和输出。您可以配置使用 Whisper 等模型的输入音频转录，设置语言偏好，并提供转录提示以提高特定领域术语的准确性。轮流检测设置控制智能体何时开始和停止响应，包括语音活动检测阈值、静音持续时间和检测到的语音周围的填充选项。

## 工具和函数

### 添加工具

就像常规智能体一样，实时智能体支持在对话期间执行的函数工具：

```python
from agents import function_tool

@function_tool
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    # Your weather API logic here
    return f"The weather in {city} is sunny, 72°F"

@function_tool
def book_appointment(date: str, time: str, service: str) -> str:
    """Book an appointment."""
    # Your booking logic here
    return f"Appointment booked for {service} on {date} at {time}"

agent = RealtimeAgent(
    name="Assistant",
    instructions="You can help with weather and appointments.",
    tools=[get_weather, book_appointment],
)
```

## 交接

### 创建交接

交接允许在专门智能体之间转移对话。

```python
from agents.realtime import realtime_handoff

# Specialized agents
billing_agent = RealtimeAgent(
    name="Billing Support",
    instructions="You specialize in billing and payment issues.",
)

technical_agent = RealtimeAgent(
    name="Technical Support",
    instructions="You handle technical troubleshooting.",
)

# Main agent with handoffs
main_agent = RealtimeAgent(
    name="Customer Service",
    instructions="You are the main customer service agent. Hand off to specialists when needed.",
    handoffs=[
        realtime_handoff(billing_agent, tool_description="Transfer to billing support"),
        realtime_handoff(technical_agent, tool_description="Transfer to technical support"),
    ]
)
```

## 事件处理

会话流式传输事件，您可以通过迭代会话对象来监听这些事件。事件包括音频输出块、转录结果、工具执行开始和结束、智能体交接和错误。需要处理的关键事件包括：

-   **audio**：来自智能体响应的原始音频数据
-   **audio_end**：智能体结束发言
-   **audio_interrupted**：用户打断智能体
-   **tool_start/tool_end**：工具执行生命周期
-   **handoff**：发生智能体交接
-   **error**：处理过程中发生错误

有关完整的事件详情，请参阅 [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent]。

## 护栏功能

实时智能体仅支持输出护栏。这些护栏会进行防抖处理并定期运行（不是每个词都运行），以避免实时生成期间的性能问题。默认的防抖长度是 100 个字符，但这是可配置的。

护栏可以直接附加到 `RealtimeAgent`，也可以通过会话的 `run_config` 提供。来自两个来源的护栏会一起运行。

```python
from agents.guardrail import GuardrailFunctionOutput, OutputGuardrail

def sensitive_data_check(context, agent, output):
    return GuardrailFunctionOutput(
        tripwire_triggered="password" in output,
        output_info=None,
    )

agent = RealtimeAgent(
    name="Assistant",
    instructions="...",
    output_guardrails=[OutputGuardrail(guardrail_function=sensitive_data_check)],
)
```

当护栏被触发时，它会生成一个 `guardrail_tripped` 事件，并可以中断智能体的当前响应。防抖行为有助于在安全性和实时性能需求之间取得平衡。与文本智能体不同，实时智能体在护栏被触发时**不会**引发异常。

## 音频处理

使用 [`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] 向会话发送音频，或使用 [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] 发送文本。

对于音频输出，监听 `audio` 事件并通过您首选的音频库播放音频数据。确保监听 `audio_interrupted` 事件，以便在用户打断智能体时立即停止播放并清除任何排队的音频。

## 直接模型访问

您可以访问底层模型以添加自定义监听器或执行高级操作：

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

这使您可以直接访问 [`RealtimeModel`][agents.realtime.model.RealtimeModel] 接口，用于需要更底层连接控制的高级用例。

## 示例

有关完整的工作示例，请查看 [examples/realtime 目录](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)，其中包含带 UI 组件和不带 UI 组件的演示。