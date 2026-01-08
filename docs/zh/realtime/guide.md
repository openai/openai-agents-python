---
search:
  exclude: true
---
# 指南

本指南深入介绍如何使用 OpenAI Agents SDK 的实时功能来构建语音能力的 AI 智能体。

!!! warning "测试版功能"
实时智能体处于测试阶段。随着实现的改进，可能会发生不兼容变更。

## 概述

实时智能体支持对话式流程，实时处理音频与文本输入，并以实时音频进行响应。它们与 OpenAI 的 Realtime API 保持持久连接，实现低延迟、可自然打断的语音对话体验。

## 架构

### 核心组件

实时系统由以下关键组件构成：

-   **RealtimeAgent**: 一个智能体，通过 instructions、tools 和 handoffs 进行配置。
-   **RealtimeRunner**: 管理配置。你可以调用 `runner.run()` 获取会话。
-   **RealtimeSession**: 单次交互会话。通常在每次用户开始对话时创建，并在对话结束前保持存活。
-   **RealtimeModel**: 底层模型接口（通常是 OpenAI 的 WebSocket 实现）

### 会话流程

典型的实时会话流程如下：

1. **创建 RealtimeAgent**，并配置 instructions、tools 和 handoffs。
2. **设置 RealtimeRunner**，提供智能体与相关配置项
3. **启动会话**，使用 `await runner.run()` 获取一个 RealtimeSession。
4. **发送音频或文本消息**，使用 `send_audio()` 或 `send_message()`
5. **监听事件**，通过遍历会话对象来接收事件——包括音频输出、转写文本、工具调用、任务转移以及错误
6. **处理打断**，当用户在智能体说话时开口，会自动停止当前音频生成

会话会维护对话历史，并管理与实时模型的持久连接。

## 智能体配置

RealtimeAgent 的工作方式与常规 Agent 类似，但有一些关键差异。完整 API 详情见 [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] API 参考。

与常规智能体的主要区别：

-   模型选择在会话级配置，而非智能体级。
-   不支持 structured outputs（不支持 `outputType`）。
-   可为每个智能体配置语音，但在首个智能体说话后不可更改。
-   其他功能如 tools、handoffs 与 instructions 的用法一致。

## 会话配置

### 模型设置

会话配置允许你控制底层实时模型的行为。你可以配置模型名称（如 `gpt-realtime`）、语音选择（alloy、echo、fable、onyx、nova、shimmer），以及支持的模态（文本和/或音频）。音频格式可分别为输入与输出设置，默认是 PCM16。

### 音频配置

音频设置控制会话如何处理语音输入与输出。你可以使用如 Whisper 的模型进行输入音频转写，设置语言偏好，并提供转写提示以提升特定领域术语的准确性。轮次检测设置控制智能体何时开始和停止响应，可配置语音活动检测阈值、静音时长以及检测语音两侧的填充。

## 工具与函数

### 添加工具

与常规智能体相同，实时智能体支持在对话中执行的工具调用：

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

会话会流式推送事件，你可以通过遍历会话对象进行监听。事件包括音频输出分片、转写结果、工具执行的开始与结束、智能体任务转移以及错误。关键事件包括：

-   **audio**: 来自智能体响应的原始音频数据
-   **audio_end**: 智能体完成发声
-   **audio_interrupted**: 用户打断了智能体
-   **tool_start/tool_end**: 工具执行的生命周期
-   **handoff**: 发生了智能体任务转移
-   **error**: 处理过程中出现错误

完整事件详情见 [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent]。

## 安全防护措施

实时智能体仅支持输出安全防护措施。这些防护是“防抖”的，并且定期运行（不会对每个词都执行），以避免实时生成中的性能问题。默认防抖长度为 100 个字符，可配置。

安全防护措施可直接附加到 `RealtimeAgent`，或通过会话的 `run_config` 提供。两处提供的防护会共同生效。

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

当安全防护被触发时，会生成 `guardrail_tripped` 事件，并可打断智能体当前响应。防抖行为有助于在安全性与实时性能要求之间取得平衡。与文本智能体不同，实时智能体在触发安全防护时**不会**抛出 Exception。

## 音频处理

使用 [`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] 发送音频到会话，或使用 [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] 发送文本。

对于音频输出，监听 `audio` 事件，并使用你的音频库播放数据。务必监听 `audio_interrupted` 事件，以便在用户打断时立即停止播放并清空任何待播的音频。

## SIP 集成

你可以将实时智能体连接到通过 [Realtime Calls API](https://platform.openai.com/docs/guides/realtime-sip) 接入的电话呼入。SDK 提供了 [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel]，在通过 SIP 协商媒体的同时复用相同的智能体流程。

要使用它，将该模型实例传入 runner，并在启动会话时提供 SIP 的 `call_id`。来电 ID 由指示来电的 webhook 传递。

```python
from agents.realtime import RealtimeAgent, RealtimeRunner
from agents.realtime.openai_realtime import OpenAIRealtimeSIPModel

runner = RealtimeRunner(
    starting_agent=agent,
    model=OpenAIRealtimeSIPModel(),
)

async with await runner.run(
    model_config={
        "call_id": call_id_from_webhook,
        "initial_model_settings": {
            "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
        },
    },
) as session:
    async for event in session:
        ...
```

当主叫挂断时，SIP 会话结束，实时连接会自动关闭。完整电话集成示例见 [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip)。

## 直接模型访问

你可以访问底层模型以添加自定义监听器或执行高级操作：

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

这使你可以直接访问 [`RealtimeModel`][agents.realtime.model.RealtimeModel] 接口，用于需要更低层连接控制的高级用例。

## 代码示例

完整可运行示例请参阅 [examples/realtime 目录](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)，包含带 UI 与不带 UI 的演示。