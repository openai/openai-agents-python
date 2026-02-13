---
search:
  exclude: true
---
# 指南

本指南深入介绍如何使用 OpenAI Agents SDK 的 realtime 能力构建支持语音的 AI 智能体。

!!! warning "Beta 功能"
Realtime 智能体处于 beta 阶段。随着我们改进实现，可能会有一些破坏性变更。

## 概览

Realtime 智能体支持对话式流程，可实时处理音频与文本输入，并以 realtime 音频进行响应。它们与 OpenAI 的 Realtime API 保持持久连接，从而实现低延迟的自然语音对话，并能优雅地处理打断。

## 架构

### 核心组件

realtime 系统由若干关键组件组成：

-   **RealtimeAgent**：一个智能体，使用 instructions、tools 和 handoffs 进行配置。
-   **RealtimeRunner**：管理配置。你可以调用 `runner.run()` 获取一个会话。
-   **RealtimeSession**：一次交互会话。通常在用户开始对话时创建一个，并保持存活直到对话结束。
-   **RealtimeModel**：底层模型接口（通常是 OpenAI 的 WebSocket 实现）

### 会话流程

一个典型的 realtime 会话遵循以下流程：

1. 使用 instructions、tools 和 handoffs **创建你的 RealtimeAgent**。
2. 使用该智能体与配置选项 **设置 RealtimeRunner**
3. 使用 `await runner.run()` **启动会话**，该调用会返回一个 RealtimeSession。
4. 使用 `send_audio()` 或 `send_message()` 向会话 **发送音频或文本消息**
5. 通过迭代 session **监听事件**——事件包括音频输出、转写文本、工具调用、任务转移以及错误
6. 在用户抢话时 **处理打断**，系统会自动停止当前音频生成

会话会维护对话历史，并管理与 realtime 模型之间的持久连接。

## 智能体配置

RealtimeAgent 与常规 Agent 类工作方式类似，但有一些关键差异。完整 API 详情见 [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] API 参考。

与常规智能体的主要差异：

-   模型选择在会话级别配置，而不是在智能体级别。
-   不支持 structured outputs（不支持 `outputType`）。
-   Voice 可按智能体配置，但在第一个智能体开口后无法更改。
-   其他特性（如工具、任务转移和 instructions）工作方式相同。

## 会话配置

### 模型设置

会话配置允许你控制底层 realtime 模型的行为。你可以配置模型名称（例如 `gpt-realtime`）、voice 选择（alloy、echo、fable、onyx、nova、shimmer）以及支持的模态（文本和/或音频）。输入与输出的音频格式都可设置，默认是 PCM16。

### 音频配置

音频设置用于控制会话如何处理语音输入与输出。你可以使用 Whisper 等模型配置输入音频转写、设置语言偏好，并提供转写提示词以提升特定领域术语的准确率。Turn detection 设置用于控制智能体何时开始和停止响应，可配置语音活动检测阈值、静音时长，以及检测到的语音前后 padding。

你还可以在 `RealtimeRunner(config=...)` 上设置的其他配置选项包括：

-   `model_settings.output_modalities`：将输出限制为文本和/或音频。
-   `model_settings.input_audio_noise_reduction`：为近场或远场音频调节降噪。
-   `guardrails_settings.debounce_text_length`：控制输出安全防护措施运行的频率。
-   `async_tool_calls`：并发运行工具调用。
-   `tool_error_formatter`：自定义模型可见的工具错误消息。

完整的类型化配置见 [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] 和 [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings]。

## 工具与函数

### 添加工具

与常规智能体一样，realtime 智能体支持在对话中执行的工具调用：

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

任务转移允许在专门的智能体之间转接对话。

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

会话会流式传输事件，你可以通过迭代 session 对象来监听。事件包括音频输出分片、转写结果、工具执行开始与结束、智能体任务转移以及错误。需要处理的关键事件包括：

-   **audio**：来自智能体响应的原始音频数据
-   **audio_end**：智能体说完
-   **audio_interrupted**：用户打断了智能体
-   **tool_start/tool_end**：工具执行生命周期
-   **handoff**：发生了智能体任务转移
-   **error**：处理过程中发生错误

完整事件详情见 [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent]。

## 安全防护措施

realtime 智能体仅支持输出安全防护措施。这些安全防护措施采用防抖机制并周期性运行（而非每个词都运行），以避免实时生成过程中的性能问题。默认防抖长度为 100 个字符，但可配置。

安全防护措施可以直接附加到 `RealtimeAgent`，也可以通过会话的 `run_config` 提供。两种来源的安全防护措施会一起运行。

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

当触发安全防护措施时，会生成 `guardrail_tripped` 事件，并可能中断智能体当前的响应。防抖行为有助于在安全性与实时性能要求之间取得平衡。与文本智能体不同，realtime 智能体在安全防护措施被触发时**不会**抛出 Exception。

## 音频处理

使用 [`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] 向会话发送音频，或使用 [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] 发送文本。

对于音频输出，监听 `audio` 事件并通过你偏好的音频库播放音频数据。务必监听 `audio_interrupted` 事件，在用户打断智能体时立即停止播放并清空任何已排队的音频。

## SIP 集成

你可以将 realtime 智能体附加到通过 [Realtime Calls API](https://platform.openai.com/docs/guides/realtime-sip) 接入的电话呼叫上。SDK 提供了 [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel]，它在通过 SIP 协商媒体的同时复用相同的智能体流程。

使用方式是：将该模型实例传给 runner，并在启动会话时提供 SIP 的 `call_id`。该 call ID 由 webhook 传递，webhook 用于指示有来电接入。

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

当来电方挂断时，SIP 会话结束，realtime 连接会自动关闭。完整的电话示例见 [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip)。

## 直接访问模型

你可以访问底层模型以添加自定义监听器或执行高级操作：

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

这将为你提供对 [`RealtimeModel`][agents.realtime.model.RealtimeModel] 接口的直接访问，以支持需要更低层级连接控制的高级用例。

## 代码示例

如需完整可运行的代码示例，请查看 [examples/realtime directory](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)，其中包含带 UI 与不带 UI 组件的演示。

## Azure OpenAI 端点格式

连接到 Azure OpenAI 时，请使用 GA Realtime 端点格式，并通过 `model_config` 中的 headers 传递凭据：

```python
model_config = {
    "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
    "headers": {"api-key": "<your-azure-api-key>"},
}
```

对于基于 token 的认证，在 `headers` 中使用 `{"authorization": f"Bearer {token}"}`。