---
search:
  exclude: true
---
# Realtime 智能体指南

本指南说明 OpenAI Agents SDK 的实时层如何映射到 OpenAI Realtime API，以及 Python SDK 在其之上增加了哪些额外行为。

!!! warning "Beta 功能"

    Realtime 智能体处于 beta 阶段。随着我们改进实现，预计会有一些破坏性变更。

!!! note "从这里开始"

    如果你想走默认的 Python 路径，请先阅读[快速开始](quickstart.md)。如果你在决定应用应使用服务端 WebSocket 还是 SIP，请阅读[Realtime 传输](transport.md)。浏览器 WebRTC 传输不属于 Python SDK 的一部分。

## 概览

Realtime 智能体会保持与 Realtime API 的长连接，使模型能够增量处理文本和音频、流式传输音频输出、调用工具，并在不中断每轮都重新发起新请求的情况下处理打断。

SDK 的主要组件有：

-   **RealtimeAgent**：一个实时专用智能体的 instructions、tools、输出安全防护措施和任务转移
-   **RealtimeRunner**：会话工厂，将起始智能体连接到实时传输
-   **RealtimeSession**：实时会话，发送输入、接收事件、追踪历史并执行工具
-   **RealtimeModel**：传输抽象。默认是 OpenAI 的服务端 WebSocket 实现。

## 会话生命周期

一个典型的实时会话如下：

1. 创建一个或多个 `RealtimeAgent`。
2. 使用起始智能体创建 `RealtimeRunner`。
3. 调用 `await runner.run()` 获取 `RealtimeSession`。
4. 通过 `async with session:` 或 `await session.enter()` 进入会话。
5. 使用 `send_message()` 或 `send_audio()` 发送用户输入。
6. 迭代处理会话事件，直到对话结束。

与纯文本运行不同，`runner.run()` 不会立即产生最终结果。它会返回一个实时会话对象，该对象会让本地历史、后台工具执行、安全防护状态以及活动智能体配置与传输层保持同步。

默认情况下，`RealtimeRunner` 使用 `OpenAIRealtimeWebSocketModel`，因此默认 Python 路径是到 Realtime API 的服务端 WebSocket 连接。如果你传入不同的 `RealtimeModel`，相同的会话生命周期和智能体功能仍然适用，但连接机制可发生变化。

## 智能体与会话配置

`RealtimeAgent` 有意比常规 `Agent` 类型更精简：

-   模型选择在会话级配置，而不是每个智能体配置。
-   不支持 structured outputs。
-   可以配置语音，但会话一旦已经产生语音输出后就不能再更改。
-   instructions、工具调用、任务转移、hooks 和输出安全防护措施仍然可用。

`RealtimeSessionModelSettings` 同时支持较新的嵌套 `audio` 配置和较旧的扁平别名。新代码优先使用嵌套结构：

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "model_name": "gpt-realtime",
            "audio": {
                "input": {
                    "format": "pcm16",
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                    "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
                },
                "output": {"format": "pcm16", "voice": "ash"},
            },
            "tool_choice": "auto",
        }
    },
)
```

有用的会话级设置包括：

-   `audio.input.format`, `audio.output.format`
-   `audio.input.transcription`
-   `audio.input.noise_reduction`
-   `audio.input.turn_detection`
-   `audio.output.voice`, `audio.output.speed`
-   `output_modalities`
-   `tool_choice`
-   `prompt`
-   `tracing`

`RealtimeRunner(config=...)` 上有用的运行级设置包括：

-   `async_tool_calls`
-   `output_guardrails`
-   `guardrails_settings.debounce_text_length`
-   `tool_error_formatter`
-   `tracing_disabled`

完整的类型化接口请参见 [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] 和 [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings]。

## 输入与输出

### 文本与结构化用户消息

对纯文本或结构化实时消息，使用 [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message]。

```python
from agents.realtime import RealtimeUserInputMessage

await session.send_message("Summarize what we discussed so far.")

message: RealtimeUserInputMessage = {
    "type": "message",
    "role": "user",
    "content": [
        {"type": "input_text", "text": "Describe this image."},
        {"type": "input_image", "image_url": image_data_url, "detail": "high"},
    ],
}
await session.send_message(message)
```

结构化消息是在实时对话中包含图像输入的主要方式。[`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) 中的示例 Web 演示就是通过这种方式转发 `input_image` 消息的。

### 音频输入

使用 [`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio] 流式发送原始音频字节：

```python
await session.send_audio(audio_bytes)
```

如果禁用了服务端回合检测，你需要自行标记回合边界。高层便捷方式是：

```python
await session.send_audio(audio_bytes, commit=True)
```

如果你需要更底层的控制，也可以通过底层模型传输发送原始客户端事件，例如 `input_audio_buffer.commit`。

### 手动响应控制

`session.send_message()` 通过高层路径发送用户输入，并为你启动响应。原始音频缓冲在所有配置下都**不会**自动执行同样的操作。

在 Realtime API 层面，手动回合控制意味着通过原始 `session.update` 清空 `turn_detection`，然后自行发送 `input_audio_buffer.commit` 和 `response.create`。

如果你在手动管理回合，可以通过模型传输发送原始客户端事件：

```python
from agents.realtime.model_inputs import RealtimeModelSendRawMessage

await session.model.send_event(
    RealtimeModelSendRawMessage(
        message={
            "type": "response.create",
        }
    )
)
```

该模式适用于以下情况：

-   `turn_detection` 已禁用，且你希望自行决定何时让模型响应
-   你希望在触发响应前检查或拦截用户输入
-   你需要为带外响应使用自定义提示词

[`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) 中的 SIP 示例使用了原始 `response.create` 来强制发送开场问候。

## 事件、历史与打断

`RealtimeSession` 会发出更高层的 SDK 事件，同时在你需要时仍会转发原始模型事件。

高价值会话事件包括：

-   `audio`, `audio_end`, `audio_interrupted`
-   `agent_start`, `agent_end`
-   `tool_start`, `tool_end`, `tool_approval_required`
-   `handoff`
-   `history_added`, `history_updated`
-   `guardrail_tripped`
-   `input_audio_timeout_triggered`
-   `error`
-   `raw_model_event`

对 UI 状态最有用的事件通常是 `history_added` 和 `history_updated`。它们会以 `RealtimeItem` 对象暴露会话的本地历史，包括用户消息、助手消息和工具调用。

### 打断与播放追踪

当用户打断助手时，会话会发出 `audio_interrupted`，并更新历史，使服务端对话与用户实际听到的内容保持一致。

在低延迟本地播放中，默认播放追踪器通常已足够。在远程或延迟播放场景（尤其是电话场景）中，请使用 [`RealtimePlaybackTracker`][agents.realtime.model.RealtimePlaybackTracker]，这样打断截断会基于实际播放进度，而不是假设所有生成音频都已被听到。

[`examples/realtime/twilio/twilio_handler.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio/twilio_handler.py) 的 Twilio 示例展示了这种模式。

## 工具、审批、任务转移与安全防护措施

### 工具调用

Realtime 智能体支持在实时对话中使用工具调用：

```python
from agents import function_tool


@function_tool
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    return f"The weather in {city} is sunny, 72F."


agent = RealtimeAgent(
    name="Assistant",
    instructions="You can answer weather questions.",
    tools=[get_weather],
)
```

### 工具审批

工具调用可在执行前要求人工审批。发生这种情况时，会话会发出 `tool_approval_required`，并暂停工具运行，直到你调用 `approve_tool_call()` 或 `reject_tool_call()`。

```python
async for event in session:
    if event.type == "tool_approval_required":
        await session.approve_tool_call(event.call_id)
```

一个具体的服务端审批循环请参见 [`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py)。human-in-the-loop 文档也在[Human in the loop](../human_in_the_loop.md)中回指这一流程。

### 任务转移

Realtime 任务转移允许一个智能体将实时对话转交给另一个专长智能体：

```python
from agents.realtime import RealtimeAgent, realtime_handoff

billing_agent = RealtimeAgent(
    name="Billing Support",
    instructions="You specialize in billing issues.",
)

main_agent = RealtimeAgent(
    name="Customer Service",
    instructions="Triage the request and hand off when needed.",
    handoffs=[realtime_handoff(billing_agent, tool_description="Transfer to billing support")],
)
```

裸 `RealtimeAgent` 任务转移会自动包装，而 `realtime_handoff(...)` 可让你自定义名称、描述、校验、回调和可用性。Realtime 任务转移**不**支持常规任务转移的 `input_filter`。

### 安全防护措施

Realtime 智能体仅支持输出安全防护措施。它们基于去抖后的转录累积运行，而不是对每个部分 token 运行，并且会发出 `guardrail_tripped` 而不是抛出异常。

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

## SIP 与电话通信

Python SDK 通过 [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] 提供一流的 SIP 附加流程。

当来电通过 Realtime Calls API 到达，且你希望将智能体会话附加到生成的 `call_id` 时使用它：

```python
from agents.realtime import RealtimeRunner
from agents.realtime.openai_realtime import OpenAIRealtimeSIPModel

runner = RealtimeRunner(starting_agent=agent, model=OpenAIRealtimeSIPModel())

async with await runner.run(
    model_config={
        "call_id": call_id_from_webhook,
    }
) as session:
    async for event in session:
        ...
```

如果你需要先接听电话，并希望接听载荷与智能体派生的会话配置一致，请使用 `OpenAIRealtimeSIPModel.build_initial_session_payload(...)`。完整流程见 [`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py)。

## 底层访问与自定义端点

你可以通过 `session.model` 访问底层传输对象。

在以下情况使用：

-   通过 `session.model.add_listener(...)` 添加自定义监听器
-   发送原始客户端事件，如 `response.create` 或 `session.update`
-   通过 `model_config` 处理自定义 `url`、`headers` 或 `api_key`
-   通过 `call_id` 附加到现有实时通话

`RealtimeModelConfig` 支持：

-   `api_key`
-   `url`
-   `headers`
-   `initial_model_settings`
-   `playback_tracker`
-   `call_id`

本仓库内置的 `call_id` 示例是 SIP。更广泛的 Realtime API 也会在某些服务端控制流程中使用 `call_id`，但这里未将其打包为 Python 示例。

连接到 Azure OpenAI 时，请传入 GA Realtime 端点 URL 和显式 headers。例如：

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"api-key": "<your-azure-api-key>"},
    }
)
```

对于基于 token 的认证，请在 `headers` 中使用 bearer token：

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"authorization": f"Bearer {token}"},
    }
)
```

如果你传入 `headers`，SDK 不会自动添加 `Authorization`。在 realtime 智能体中避免使用旧版 beta 路径（`/openai/realtime?api-version=...`）。

## 延伸阅读

-   [Realtime 传输](transport.md)
-   [快速开始](quickstart.md)
-   [OpenAI Realtime 对话](https://developers.openai.com/api/docs/guides/realtime-conversations/)
-   [OpenAI Realtime 服务端控制](https://developers.openai.com/api/docs/guides/realtime-server-controls/)
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)