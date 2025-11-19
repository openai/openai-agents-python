---
search:
  exclude: true
---
# 指南

本指南将深入介绍如何使用 OpenAI Agents SDK 的实时功能来构建语音驱动的 AI 智能体。

!!! warning "Beta feature"
实时智能体处于 Beta 阶段。随着实现的改进，可能会发生不兼容的变更。

## 概览

实时智能体支持会话式流程，可实时处理音频与文本输入，并以实时音频作出响应。它们与 OpenAI 的 Realtime API 保持持久连接，从而实现低延迟的自然语音对话，并能优雅地处理打断。

## 架构

### 核心组件

实时系统由以下关键组件组成：

-   **RealtimeAgent**: 一个智能体，通过 instructions、tools 和 任务转移 进行配置。
-   **RealtimeRunner**: 管理配置。你可以调用 `runner.run()` 获取一个会话。
-   **RealtimeSession**: 单次交互会话。通常在每次用户发起对话时创建一个，并保持其存活直到对话结束。
-   **RealtimeModel**: 底层模型接口（通常是 OpenAI 的 WebSocket 实现）

### 会话流程

典型的实时会话流程如下：

1. **创建你的 RealtimeAgent**，并配置 instructions、tools 和 任务转移。
2. **设置 RealtimeRunner**，传入智能体和配置选项
3. **启动会话**，使用 `await runner.run()` 返回一个 RealtimeSession。
4. **向会话发送音频或文本消息**，使用 `send_audio()` 或 `send_message()`
5. **监听事件**，通过迭代会话对象获取事件——包括音频输出、转写、工具调用、任务转移以及错误
6. **处理打断**，当用户打断发言时，会自动停止当前音频生成

会话会维护对话历史，并管理与实时模型的持久连接。

## 智能体配置

RealtimeAgent 的工作方式与常规 Agent 类似，但有一些关键差异。完整 API 详情参见 [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] API 参考。

与常规智能体的主要差异：

-   模型选择在会话级别配置，而非智能体级别。
-   不支持 structured outputs（不支持 `outputType`）。
-   语音可按智能体配置，但在第一个智能体开始说话后无法更改。
-   其他特性如 tools、任务转移 和 instructions 的工作方式相同。

## 会话配置

### 模型设置

会话配置允许你控制底层实时模型的行为。你可以配置模型名称（如 `gpt-realtime`）、语音选择（alloy、echo、fable、onyx、nova、shimmer），以及支持的模态（文本和/或音频）。可为输入和输出设置音频格式，默认是 PCM16。

### 音频配置

音频设置控制会话如何处理语音输入与输出。你可以使用诸如 Whisper 的模型配置输入音频转写、设置语言偏好，并提供转写提示以提升特定领域术语的准确性。轮次检测设置控制智能体何时开始与停止响应，可配置语音活动检测阈值、静音时长以及检测语音的前后填充。

## 工具与函数

### 添加工具

与常规智能体一样，实时智能体支持在对话中执行的 工具调用：

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

任务转移允许在专业化智能体之间转移对话。

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

会话会以流式方式发送事件，你可以通过迭代会话对象来监听。事件包括音频输出分片、转写结果、工具执行开始与结束、智能体任务转移以及错误。需要重点处理的事件包括：

-   **audio**: 来自智能体响应的原始音频数据
-   **audio_end**: 智能体结束发言
-   **audio_interrupted**: 用户打断了智能体
-   **tool_start/tool_end**: 工具执行的生命周期
-   **handoff**: 发生了智能体任务转移
-   **error**: 处理过程中发生错误

完整事件详情参见 [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent]。

## 安全防护措施

实时智能体仅支持输出安全防护措施。这些安全防护措施会进行防抖并定期运行（不是每个词都执行），以避免实时生成过程中的性能问题。默认防抖长度为 100 个字符，但可配置。

安全防护措施可直接附加到 `RealtimeAgent`，也可通过会话的 `run_config` 提供。来自两处的防护会共同生效。

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

当安全防护措施被触发时，会生成 `guardrail_tripped` 事件，并可打断智能体当前的响应。防抖行为有助于在安全性与实时性能之间取得平衡。与文本智能体不同，实时智能体在触发安全防护措施时**不会**抛出异常。

## 音频处理

使用 [`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] 发送音频到会话，或使用 [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] 发送文本。

对于音频输出，监听 `audio` 事件并通过你偏好的音频库播放音频数据。务必监听 `audio_interrupted` 事件，以便在用户打断智能体时立即停止播放，并清空任何已排队的音频。

## SIP 集成

你可以将实时智能体附加到通过 [Realtime Calls API](https://platform.openai.com/docs/guides/realtime-sip) 到达的电话呼叫。SDK 提供了 [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel]，在通过 SIP 协商媒体的同时复用相同的智能体流程。

要使用它，请将模型实例传给 runner，并在启动会话时提供 SIP 的 `call_id`。呼叫 ID 由指示来电的 webhook 传递。

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

当主叫挂断时，SIP 会话结束，实时连接会自动关闭。完整的电话集成示例参见 [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip)。

## 直接访问模型

你可以访问底层模型以添加自定义监听器或执行高级操作：

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

这将为你提供对 [`RealtimeModel`][agents.realtime.model.RealtimeModel] 接口的直接访问，适用于需要更低层连接控制的高级用例。

## 示例

完整可运行示例请查看 [examples/realtime 目录](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)，其中包含带 UI 和不带 UI 的演示。