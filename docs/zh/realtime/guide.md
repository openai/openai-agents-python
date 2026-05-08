---
search:
  exclude: true
---
# Realtime 智能体指南

本指南说明 OpenAI Agents SDK 的 realtime 层如何映射到 OpenAI Realtime API，以及 Python SDK 在其之上添加了哪些额外行为。

!!! warning "Beta 功能"

    Realtime 智能体处于 beta 阶段。随着我们改进实现，预计会有一些破坏性变更。

!!! note "从这里开始"

    如果你想使用默认的 Python 路径，请先阅读[快速入门](quickstart.md)。如果你正在决定你的应用应使用服务端 WebSocket 还是 SIP，请阅读 [Realtime 传输](transport.md)。浏览器 WebRTC 传输不属于 Python SDK 的一部分。

## 概述

Realtime 智能体会与 Realtime API 保持长连接，以便模型能够增量处理文本和音频、流式传输音频输出、调用工具，并在每一轮无需重新启动新请求的情况下处理中断。

主要的 SDK 组件包括：

-   **RealtimeAgent**: 单个 realtime 专家的 instructions、工具、输出安全防护措施和任务转移
-   **RealtimeRunner**: 将起始智能体连接到 realtime 传输的会话工厂
-   **RealtimeSession**: 用于发送输入、接收事件、跟踪历史并执行工具的实时会话
-   **RealtimeModel**: 传输抽象。默认值是 OpenAI 的服务端 WebSocket 实现。

## 会话生命周期

典型的 realtime 会话如下：

1. 创建一个或多个 `RealtimeAgent`。
2. 使用起始智能体创建一个 `RealtimeRunner`。
3. 调用 `await runner.run()` 以获取 `RealtimeSession`。
4. 使用 `async with session:` 或 `await session.enter()` 进入会话。
5. 使用 `send_message()` 或 `send_audio()` 发送用户输入。
6. 迭代会话事件，直到对话结束。

与纯文本运行不同，`runner.run()` 不会立即生成最终结果。它会返回一个实时会话对象，该对象会让本地历史、后台工具执行、安全防护措施状态以及活动智能体配置与传输层保持同步。

默认情况下，`RealtimeRunner` 使用 `OpenAIRealtimeWebSocketModel`，因此默认的 Python 路径是连接到 Realtime API 的服务端 WebSocket 连接。如果传入不同的 `RealtimeModel`，相同的会话生命周期和智能体功能仍然适用，但连接机制可能会改变。

## 智能体和会话配置

`RealtimeAgent` 有意比常规 `Agent` 类型更窄：

-   模型选择在会话级别配置，而不是按智能体配置。
-   不支持 structured outputs。
-   可以配置语音，但在会话已经生成过语音音频后就无法更改。
-   Instructions、工具调用、任务转移、钩子和输出安全防护措施仍然都可用。

`RealtimeSessionModelSettings` 同时支持较新的嵌套 `audio` 配置和较旧的扁平别名。新代码建议使用嵌套形式，并从 `gpt-realtime-2` 开始构建新的 realtime 智能体：

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "model_name": "gpt-realtime-2",
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

完整的类型化接口请参阅 [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] 和 [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings]。

## 输入和输出

### 文本和结构化用户消息

使用 [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] 发送纯文本或结构化 realtime 消息。

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

结构化消息是在 realtime 对话中包含图像输入的主要方式。[`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) 中的示例 Web 演示会以这种方式转发 `input_image` 消息。

### 音频输入

使用 [`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio] 流式传输原始音频字节：

```python
await session.send_audio(audio_bytes)
```

如果服务端轮次检测被禁用，你需要负责标记轮次边界。高级便捷方法是：

```python
await session.send_audio(audio_bytes, commit=True)
```

如果需要更低级别的控制，也可以通过底层模型传输发送原始客户端事件，例如 `input_audio_buffer.commit`。

### 手动响应控制

`session.send_message()` 会使用高级路径发送用户输入，并为你启动响应。原始音频缓冲并**不会**在每种配置中都自动执行相同操作。

在 Realtime API 层面，手动轮次控制意味着用原始 `session.update` 清除 `turn_detection`，然后自行发送 `input_audio_buffer.commit` 和 `response.create`。

如果你正在手动管理轮次，可以通过模型传输发送原始客户端事件：

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

此模式在以下情况下很有用：

-   `turn_detection` 被禁用，并且你想决定模型何时响应
-   你想在触发响应前检查或拦截用户输入
-   你需要为带外响应使用自定义提示词

[`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) 中的 SIP 示例使用原始 `response.create` 来强制发送开场问候。

## 事件、历史和中断

`RealtimeSession` 会发出更高层级的 SDK 事件，同时在你需要时仍会转发原始模型事件。

高价值的会话事件包括：

-   `audio`, `audio_end`, `audio_interrupted`
-   `agent_start`, `agent_end`
-   `tool_start`, `tool_end`, `tool_approval_required`
-   `handoff`
-   `history_added`, `history_updated`
-   `guardrail_tripped`
-   `input_audio_timeout_triggered`
-   `error`
-   `raw_model_event`

对 UI 状态最有用的事件通常是 `history_added` 和 `history_updated`。它们会将会话的本地历史作为 `RealtimeItem` 对象公开，包括用户消息、助手消息和工具调用。

### 中断和播放跟踪

当用户打断助手时，会话会发出 `audio_interrupted` 并更新历史，使服务端对话与用户实际听到的内容保持一致。

在低延迟本地播放中，默认播放跟踪器通常已经足够。在远程或延迟播放场景中，尤其是电话场景，请使用 [`RealtimePlaybackTracker`][agents.realtime.model.RealtimePlaybackTracker]，这样中断截断会基于实际播放进度，而不是假设所有生成的音频都已经被听到。

[`examples/realtime/twilio/twilio_handler.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio/twilio_handler.py) 中的 Twilio 示例展示了此模式。

## 工具、审批、任务转移和安全防护措施

### 工具调用

Realtime 智能体支持在实时对话期间使用工具调用：

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

工具调用可以要求在执行前进行人工审批。发生这种情况时，会话会发出 `tool_approval_required` 并暂停工具运行，直到你调用 `approve_tool_call()` 或 `reject_tool_call()`。

```python
async for event in session:
    if event.type == "tool_approval_required":
        await session.approve_tool_call(event.call_id)
```

有关具体的服务端审批循环，请参阅 [`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py)。人在回路文档也会在[人在回路](../human_in_the_loop.md)中指回此流程。

### 任务转移

Realtime 任务转移允许一个智能体将实时对话转交给另一位专家：

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

裸 `RealtimeAgent` 任务转移会被自动包装，而 `realtime_handoff(...)` 允许你自定义名称、描述、验证、回调和可用性。Realtime 任务转移不支持常规任务转移 `input_filter`。

### 安全防护措施

Realtime 智能体仅支持输出安全防护措施。它们基于去抖后的转录文本累积运行，而不是在每个部分 token 上运行，并且会发出 `guardrail_tripped` 而不是抛出异常。

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

当 realtime 输出安全防护措施被触发时，会话会中断活动响应，强制执行
`response.cancel`，发出 `guardrail_tripped`，并发送一条后续用户消息来指出
被触发的安全防护措施，以便模型生成替代响应。你的音频播放器仍应
监听 `audio_interrupted` 并立即停止本地播放，因为安全防护措施基于
去抖后的转录文本运行，而在触发线触发时，可能已经缓冲了一些音频。

## SIP 和电话

Python SDK 通过 [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] 提供一流的 SIP attach 流程。

当呼叫通过 Realtime Calls API 到达，并且你想将智能体会话附加到生成的 `call_id` 时，请使用它：

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

如果需要先接听呼叫，并且希望接听 payload 与智能体派生的会话配置匹配，请使用 `OpenAIRealtimeSIPModel.build_initial_session_payload(...)`。完整流程见 [`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py)。

## 低级访问和自定义端点

你可以通过 `session.model` 访问底层传输对象。

在以下情况下使用它：

-   通过 `session.model.add_listener(...)` 使用自定义监听器
-   原始客户端事件，例如 `response.create` 或 `session.update`
-   通过 `model_config` 处理自定义 `url`、`headers` 或 `api_key`
-   将 `call_id` 附加到现有 realtime 呼叫

`RealtimeModelConfig` 支持：

-   `api_key`
-   `url`
-   `headers`
-   `initial_model_settings`
-   `playback_tracker`
-   `call_id`

此代码库随附的 `call_id` 示例是 SIP。更广泛的 Realtime API 也会在一些服务端控制流中使用 `call_id`，但这里没有将它们打包为 Python 示例。

连接到 Azure OpenAI 时，请传入 GA Realtime 端点 URL 和显式 headers。例如：

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"api-key": "<your-azure-api-key>"},
    }
)
```

对于基于 token 的身份验证，请在 `headers` 中使用 bearer token：

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"authorization": f"Bearer {token}"},
    }
)
```

如果传入 `headers`，SDK 不会自动添加 `Authorization`。避免将旧版 beta 路径（`/openai/realtime?api-version=...`）与 realtime 智能体一起使用。

## 延伸阅读

-   [Realtime 传输](transport.md)
-   [快速入门](quickstart.md)
-   [OpenAI Realtime 对话](https://developers.openai.com/api/docs/guides/realtime-conversations/)
-   [OpenAI Realtime 服务端控制](https://developers.openai.com/api/docs/guides/realtime-server-controls/)
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)