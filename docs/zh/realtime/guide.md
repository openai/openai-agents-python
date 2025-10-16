---
search:
  exclude: true
---
# 指南

本指南深入介绍如何使用 OpenAI Agents SDK 的实时功能构建支持语音的 AI 智能体。

!!! warning "测试版功能"
实时智能体目前处于测试阶段。随着我们改进实现，可能会有不兼容的变更。

## 概述

实时智能体支持对话式流程，能够实时处理音频与文本输入，并以实时音频进行响应。它们与 OpenAI 的 Realtime API 保持持久连接，从而实现低延迟的自然语音对话，并能够优雅地处理打断。

## 架构

### 核心组件

实时系统由以下关键组件组成：

-   **RealtimeAgent**: 一个智能体，可配置 instructions、tools 和 handoffs。
-   **RealtimeRunner**: 管理配置。你可以调用 `runner.run()` 获取会话。
-   **RealtimeSession**: 单次交互会话。通常在每次用户开始对话时创建一个，并在对话结束前保持存活。
-   **RealtimeModel**: 底层模型接口（通常为 OpenAI 的 WebSocket 实现）

### 会话流程

典型的实时会话流程如下：

1. **创建 RealtimeAgent**，配置 instructions、tools 和 handoffs。
2. **设置 RealtimeRunner**，提供智能体和配置选项
3. **启动会话**，使用 `await runner.run()`，返回一个 RealtimeSession。
4. **发送音频或文本消息** 到会话，使用 `send_audio()` 或 `send_message()`
5. **监听事件**，通过迭代会话对象获取事件——包括音频输出、转写、工具调用、任务转移和错误
6. **处理打断**，当用户打断智能体说话时，会自动停止当前的音频生成

会话会维护对话历史，并管理与实时模型的持久连接。

## 智能体配置

RealtimeAgent 与常规 Agent 类似，但有一些关键差异。完整 API 详情参见 [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] API 参考。

与常规智能体的主要差异：

-   模型选择在会话级别进行配置，而不是智能体级别。
-   不支持 structured output（不支持 `outputType`）。
-   语音可按智能体配置，但在第一个智能体开始说话后不可更改。
-   其它特性如工具、任务转移和 instructions 的工作方式相同。

## 会话配置

### 模型设置

会话配置允许你控制底层实时模型的行为。你可以配置模型名称（如 `gpt-realtime`）、语音选择（alloy、echo、fable、onyx、nova、shimmer）以及支持的模态（文本和/或音频）。音频格式可分别针对输入与输出设置，默认是 PCM16。

### 音频配置

音频设置控制会话如何处理语音输入与输出。你可以使用如 Whisper 的模型进行输入音频转写、设置语言偏好，并提供转写提示以提升领域术语的准确性。轮次检测设置控制智能体何时开始和停止响应，包括语音活动检测阈值、静音时长以及对检测到的语音添加前后缓冲。

## 工具与函数

### 添加工具

与常规智能体相同，实时智能体支持在对话中执行的 function tools：

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

## 任务转移

### 创建任务转移

任务转移允许在专门化智能体之间转移对话。

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

会话会以流式方式发送事件，你可以通过迭代会话对象来监听。事件包括音频输出分片、转写结果、工具执行开始和结束、智能体任务转移以及错误。需要重点处理的事件包括：

-   **audio**: 来自智能体响应的原始音频数据
-   **audio_end**: 智能体完成发声
-   **audio_interrupted**: 用户打断了智能体
-   **tool_start/tool_end**: 工具执行的生命周期
-   **handoff**: 发生智能体任务转移
-   **error**: 处理过程中发生错误

完整事件详情参见 [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent]。

## 安全防护措施

实时智能体仅支持输出安全防护措施。这些防护以去抖动方式周期性运行（非每个词触发），以避免实时生成时的性能问题。默认去抖长度为 100 个字符，可配置。

安全防护措施可以直接附加到 `RealtimeAgent`，或通过会话的 `run_config` 提供。两个来源的防护会共同生效。

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

当安全防护被触发时，会生成 `guardrail_tripped` 事件，并可中断智能体当前的响应。去抖动行为有助于在安全性与实时性能要求之间取得平衡。与文本智能体不同，实时智能体在防护被触发时不会抛出异常。

## 音频处理

通过 [`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] 向会话发送音频，或通过 [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] 发送文本。

对于音频输出，监听 `audio` 事件并通过你偏好的音频库播放音频数据。务必监听 `audio_interrupted` 事件，以便在用户打断智能体时立即停止播放并清空任何已排队的音频。

## 直接访问模型

你可以访问底层模型以添加自定义监听器或执行高级操作：

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

这使你能够直接访问 [`RealtimeModel`][agents.realtime.model.RealtimeModel] 接口，以满足需要对连接进行更底层控制的高级用例。

## 示例

完整可运行的示例请查看 [examples/realtime 目录](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)，其中包含带有和不带 UI 组件的演示。