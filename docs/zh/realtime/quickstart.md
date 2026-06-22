---
search:
  exclude: true
---
# 快速入门

Python SDK 中的实时智能体是基于 WebSocket 传输之上的 OpenAI Realtime API 构建的服务端低延迟智能体。

!!! note "Python SDK 边界"

    Python SDK **不**提供浏览器 WebRTC 传输。本页仅介绍由 Python 管理、通过服务端 WebSocket 进行的实时会话。使用此 SDK 进行服务端编排、工具、审批以及电话集成。另请参阅[实时传输](transport.md)。

## 前提条件

-   Python 3.10 或更高版本
-   OpenAI API 密钥
-   对 OpenAI Agents SDK 有基本了解

## 安装

如果尚未安装，请安装 OpenAI Agents SDK：

```bash
pip install openai-agents
```

## 服务端实时会话创建

### 1. 实时组件导入

```python
import asyncio

from agents.realtime import RealtimeAgent, RealtimeRunner
```

### 2. 起始智能体定义

```python
agent = RealtimeAgent(
    name="Assistant",
    instructions="You are a helpful voice assistant. Keep responses short and conversational.",
)
```

### 3. runner 配置

对于新代码，优先使用嵌套的 `audio.input` / `audio.output` 会话设置结构。对于新的实时智能体，请从 `gpt-realtime-2` 开始。

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
                    "turn_detection": {
                        "type": "semantic_vad",
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": "pcm16",
                    "voice": "ash",
                },
            },
        }
    },
)
```

### 4. 会话启动与输入发送

`runner.run()` 返回一个 `RealtimeSession`。当你进入会话上下文时，连接会被打开。

```python
async def main() -> None:
    session = await runner.run()

    async with session:
        await session.send_message("Say hello in one short sentence.")

        async for event in session:
            if event.type == "audio":
                # Forward or play event.audio.data.
                pass
            elif event.type == "history_added":
                print(event.item)
            elif event.type == "agent_end":
                # One assistant turn finished.
                break
            elif event.type == "error":
                print(f"Error: {event.error}")


if __name__ == "__main__":
    asyncio.run(main())
```

`session.send_message()` 接受纯字符串或结构化实时消息。对于原始音频块，请使用 [`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio]。

## 本快速入门未包含的内容

-   麦克风采集和扬声器播放代码。请参阅 [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) 中的实时代码示例。
-   SIP / 电话附加流程。请参阅[实时传输](transport.md)和 [SIP 部分](guide.md#sip-and-telephony)。

## 关键设置

基本会话运行后，大多数人接下来会用到的设置包括：

-   `model_name`
-   `audio.input.format`、`audio.output.format`
-   `audio.input.transcription`
-   `audio.input.noise_reduction`
-   `audio.input.turn_detection` 用于自动轮次检测
-   `audio.output.voice`
-   `tool_choice`、`prompt`、`tracing`
-   `async_tool_calls`、`tool_execution.pre_approval_tool_input_guardrails`、`guardrails_settings.debounce_text_length`、`tool_error_formatter`

较旧的扁平别名（如 `input_audio_format`、`output_audio_format`、`input_audio_transcription` 和 `turn_detection`）仍然可用，但新代码首选嵌套的 `audio` 设置。

对于手动轮次控制，请使用原始的 `session.update` / `input_audio_buffer.commit` / `response.create` 流程，具体如[实时智能体指南](guide.md#manual-response-control)中所述。

完整模式请参阅 [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] 和 [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings]。

## 连接选项

在环境中设置你的 API 密钥：

```bash
export OPENAI_API_KEY="your-api-key-here"
```

或在启动会话时直接传入：

```python
session = await runner.run(model_config={"api_key": "your-api-key"})
```

`model_config` 还支持：

-   `url`：自定义 WebSocket 端点
-   `headers`：自定义请求头
-   `call_id`：附加到现有实时通话。在此仓库中，已记录的附加流程是 SIP。
-   `playback_tracker`：报告用户实际听到的音频量

如果你显式传入 `headers`，SDK 将**不会**为你注入 `Authorization` 标头。

连接到 Azure OpenAI 时，请在 `model_config["url"]` 中传入 GA Realtime 端点 URL，并显式传入 headers。对于实时智能体，请避免使用旧版 beta 路径（`/openai/realtime?api-version=...`）。详情请参阅[实时智能体指南](guide.md#low-level-access-and-custom-endpoints)。

## 后续步骤

-   阅读[实时传输](transport.md)，以在服务端 WebSocket 和 SIP 之间做出选择。
-   阅读[实时智能体指南](guide.md)，了解生命周期、结构化输入、审批、任务转移、安全防护措施和底层控制。
-   浏览 [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) 中的代码示例。