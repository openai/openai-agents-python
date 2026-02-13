---
search:
  exclude: true
---
# 快速入门

Realtime 智能体支持使用 OpenAI 的 Realtime API 与你的 AI 智能体进行语音对话。本指南将带你创建第一个 realtime 语音智能体。

!!! warning "Beta 功能"
Realtime 智能体目前处于 beta 阶段。随着我们改进实现，可能会出现一些破坏性变更。

## 前置条件

-   Python 3.9 或更高版本
-   OpenAI API key
-   对 OpenAI Agents SDK 的基本了解

## 安装

如果你还没有安装 OpenAI Agents SDK，请先安装：

```bash
pip install openai-agents
```

## 创建你的第一个 realtime 智能体

### 1. 导入所需组件

```python
import asyncio
from agents.realtime import RealtimeAgent, RealtimeRunner
```

### 2. 创建一个 realtime 智能体

```python
agent = RealtimeAgent(
    name="Assistant",
    instructions="You are a helpful voice assistant. Keep your responses conversational and friendly.",
)
```

### 3. 设置 runner

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "model_name": "gpt-realtime",
            "voice": "ash",
            "modalities": ["audio"],
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": "gpt-4o-mini-transcribe"},
            "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
        }
    }
)
```

### 4. 启动会话

```python
# Start the session
session = await runner.run()

async with session:
    print("Session started! The agent will stream audio responses in real-time.")
    # Process events
    async for event in session:
        try:
            if event.type == "agent_start":
                print(f"Agent started: {event.agent.name}")
            elif event.type == "agent_end":
                print(f"Agent ended: {event.agent.name}")
            elif event.type == "handoff":
                print(f"Handoff from {event.from_agent.name} to {event.to_agent.name}")
            elif event.type == "tool_start":
                print(f"Tool started: {event.tool.name}")
            elif event.type == "tool_end":
                print(f"Tool ended: {event.tool.name}; output: {event.output}")
            elif event.type == "audio_end":
                print("Audio ended")
            elif event.type == "audio":
                # Enqueue audio for callback-based playback with metadata
                # Non-blocking put; queue is unbounded, so drops won’t occur.
                pass
            elif event.type == "audio_interrupted":
                print("Audio interrupted")
                # Begin graceful fade + flush in the audio callback and rebuild jitter buffer.
            elif event.type == "error":
                print(f"Error: {event.error}")
            elif event.type == "history_updated":
                pass  # Skip these frequent events
            elif event.type == "history_added":
                pass  # Skip these frequent events
            elif event.type == "raw_model_event":
                print(f"Raw model event: {_truncate_str(str(event.data), 200)}")
            else:
                print(f"Unknown event type: {event.type}")
        except Exception as e:
            print(f"Error processing event: {_truncate_str(str(e), 200)}")

def _truncate_str(s: str, max_length: int) -> str:
    if len(s) > max_length:
        return s[:max_length] + "..."
    return s
```

## 完整示例

下面是一个完整可运行的示例：

```python
import asyncio
from agents.realtime import RealtimeAgent, RealtimeRunner

async def main():
    # Create the agent
    agent = RealtimeAgent(
        name="Assistant",
        instructions="You are a helpful voice assistant. Keep responses brief and conversational.",
    )
    # Set up the runner with configuration
    runner = RealtimeRunner(
        starting_agent=agent,
        config={
            "model_settings": {
                "model_name": "gpt-realtime",
                "voice": "ash",
                "modalities": ["audio"],
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_transcription": {"model": "gpt-4o-mini-transcribe"},
                "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
            }
        },
    )
    # Start the session
    session = await runner.run()

    async with session:
        print("Session started! The agent will stream audio responses in real-time.")
        # Process events
        async for event in session:
            try:
                if event.type == "agent_start":
                    print(f"Agent started: {event.agent.name}")
                elif event.type == "agent_end":
                    print(f"Agent ended: {event.agent.name}")
                elif event.type == "handoff":
                    print(f"Handoff from {event.from_agent.name} to {event.to_agent.name}")
                elif event.type == "tool_start":
                    print(f"Tool started: {event.tool.name}")
                elif event.type == "tool_end":
                    print(f"Tool ended: {event.tool.name}; output: {event.output}")
                elif event.type == "audio_end":
                    print("Audio ended")
                elif event.type == "audio":
                    # Enqueue audio for callback-based playback with metadata
                    # Non-blocking put; queue is unbounded, so drops won’t occur.
                    pass
                elif event.type == "audio_interrupted":
                    print("Audio interrupted")
                    # Begin graceful fade + flush in the audio callback and rebuild jitter buffer.
                elif event.type == "error":
                    print(f"Error: {event.error}")
                elif event.type == "history_updated":
                    pass  # Skip these frequent events
                elif event.type == "history_added":
                    pass  # Skip these frequent events
                elif event.type == "raw_model_event":
                    print(f"Raw model event: {_truncate_str(str(event.data), 200)}")
                else:
                    print(f"Unknown event type: {event.type}")
            except Exception as e:
                print(f"Error processing event: {_truncate_str(str(e), 200)}")

def _truncate_str(s: str, max_length: int) -> str:
    if len(s) > max_length:
        return s[:max_length] + "..."
    return s

if __name__ == "__main__":
    # Run the session
    asyncio.run(main())
```

## 配置选项

### 模型设置

-   `model_name`: 从可用的 realtime 模型中选择（例如 `gpt-realtime`）
-   `voice`: 选择声音（`alloy`、`echo`、`fable`、`onyx`、`nova`、`shimmer`）
-   `modalities`: 启用文本或音频（`["text"]` 或 `["audio"]`）

### 音频设置

-   `input_audio_format`: 输入音频的格式（`pcm16`、`g711_ulaw`、`g711_alaw`）
-   `output_audio_format`: 输出音频的格式
-   `input_audio_transcription`: 转写配置

### 轮次检测

-   `type`: 检测方法（`server_vad`、`semantic_vad`）
-   `threshold`: 语音活动阈值（0.0-1.0）
-   `silence_duration_ms`: 用于检测轮次结束的静默时长
-   `prefix_padding_ms`: 语音前的音频填充

## 后续步骤

-   [了解更多 realtime 智能体](guide.md)
-   查看 [examples/realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) 文件夹中的可运行示例
-   为你的智能体添加工具调用
-   实现智能体之间的任务转移
-   为安全设置安全防护措施

## 身份验证

确保你的环境中已设置 OpenAI API key：

```bash
export OPENAI_API_KEY="your-api-key-here"
```

或者在创建会话时直接传入：

```python
session = await runner.run(model_config={"api_key": "your-api-key"})
```

## Azure OpenAI 端点格式

如果你连接的是 Azure OpenAI 而不是 OpenAI 的默认端点，请在
`model_config["url"]` 中传入 GA Realtime URL，并显式设置认证 headers。

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"api-key": "<your-azure-api-key>"},
    }
)
```

你也可以使用 bearer token：

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"authorization": f"Bearer {token}"},
    }
)
```

避免在 realtime 智能体中使用旧版 beta 路径（`/openai/realtime?api-version=...`）。SDK 期望使用 GA Realtime 接口。