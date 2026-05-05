---
search:
  exclude: true
---
# 快速入门

Python SDK 中的实时智能体是服务端、低延迟的智能体，基于 OpenAI Realtime API 并通过 WebSocket 传输构建。

!!! warning "Beta 功能"

    实时智能体目前处于 beta 阶段。随着我们改进实现，预计会有一些破坏性变更。

!!! note "Python SDK 边界"

    Python SDK **不**提供浏览器 WebRTC 传输。本页仅涵盖由 Python 管理、基于服务端 WebSockets 的实时会话。可使用此 SDK 进行服务端编排、工具调用、审批和电话集成。另请参见[Realtime transport](transport.md)。

## 前提条件

-   Python 3.10 或更高版本
-   OpenAI API 密钥
-   对 OpenAI Agents SDK 的基本了解

## 安装

如果你尚未安装，请安装 OpenAI Agents SDK：

```bash
pip install openai-agents
```

## 创建服务端实时会话

### 1. 导入实时组件

```python
import asyncio

from agents.realtime import RealtimeAgent, RealtimeRunner
```

### 2. 定义起始智能体

```python
agent = RealtimeAgent(
    name="Assistant",
    instructions="You are a helpful voice assistant. Keep responses short and conversational.",
)
```

### 3. 配置运行器

新代码推荐使用嵌套的 `audio.input` / `audio.output` 会话设置结构。对于新的实时智能体，建议从 `gpt-realtime-1.5` 开始。

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "model_name": "gpt-realtime-1.5",
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

### 4. 启动会话并发送输入

`runner.run()` 返回一个 `RealtimeSession`。进入会话上下文时会打开连接。

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

`session.send_message()` 既可接收纯字符串，也可接收结构化的实时消息。对于原始音频块，请使用 [`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio]。

## 本快速入门未包含的内容

-   麦克风采集和扬声器播放代码。请参阅 [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) 中的实时示例。
-   SIP / 电话接入流程。请参阅 [Realtime transport](transport.md) 和 [SIP 部分](guide.md#sip-and-telephony)。

## 关键设置

当基础会话可用后，大多数人接下来会用到这些设置：

-   `model_name`
-   `audio.input.format`, `audio.output.format`
-   `audio.input.transcription`
-   `audio.input.noise_reduction`
-   用于自动轮次检测的 `audio.input.turn_detection`
-   `audio.output.voice`
-   `tool_choice`, `prompt`, `tracing`
-   `async_tool_calls`, `guardrails_settings.debounce_text_length`, `tool_error_formatter`

较旧的扁平别名（如 `input_audio_format`、`output_audio_format`、`input_audio_transcription` 和 `turn_detection`）仍可使用，但新代码更推荐使用嵌套 `audio` 设置。

对于手动轮次控制，请使用原始 `session.update` / `input_audio_buffer.commit` / `response.create` 流程，如[Realtime agents guide](guide.md#manual-response-control)所述。

完整模式请参阅 [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] 和 [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings]。

## 连接选项

在环境中设置 API 密钥：

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
-   `call_id`：附加到现有实时通话。在本仓库中，文档化的附加流程是 SIP。
-   `playback_tracker`：报告用户实际听到了多少音频

如果你显式传入 `headers`，SDK 将**不会**为你注入 `Authorization` 请求头。

连接 Azure OpenAI 时，请在 `model_config["url"]` 中传入 GA Realtime 端点 URL，并显式设置请求头。避免在实时智能体中使用旧版 beta 路径（`/openai/realtime?api-version=...`）。详见[Realtime agents guide](guide.md#low-level-access-and-custom-endpoints)。

## 后续步骤

-   阅读 [Realtime transport](transport.md)，在服务端 WebSocket 和 SIP 之间进行选择。
-   阅读 [Realtime agents guide](guide.md)，了解生命周期、结构化输入、审批、任务转移、安全防护措施和底层控制。
-   浏览 [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) 中的示例。