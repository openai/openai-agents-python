---
search:
  exclude: true
---
# 快速入门

实时智能体通过 OpenAI 的 Realtime API 实现与你的 AI 智能体进行语音对话。本指南将带你创建第一个实时语音智能体。

!!! warning "测试版功能"
Realtime 智能体目前处于测试阶段。随着我们改进实现，可能会有不兼容的变更。

## 先决条件

- Python 3.9 或更高版本
- OpenAI API key
- 对 OpenAI Agents SDK 的基本了解

## 安装

如果尚未安装，请先安装 OpenAI Agents SDK：

```bash
pip install openai-agents
```

## 创建你的第一个实时智能体

### 1. 导入所需组件

```python
import asyncio
from agents.realtime import RealtimeAgent, RealtimeRunner
```

### 2. 创建实时智能体

```python
agent = RealtimeAgent(
    name="Assistant",
    instructions="You are a helpful voice assistant. Keep your responses conversational and friendly.",
)
```

### 3. 设置运行器

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

下面是一个完整的可运行示例：

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

- `model_name`: 从可用的实时模型中选择（例如，`gpt-realtime`）
- `voice`: 选择语音（`alloy`、`echo`、`fable`、`onyx`、`nova`、`shimmer`）
- `modalities`: 启用文本或音频（`["text"]` 或 `["audio"]`）

### 音频设置

- `input_audio_format`: 输入音频格式（`pcm16`、`g711_ulaw`、`g711_alaw`）
- `output_audio_format`: 输出音频格式
- `input_audio_transcription`: 转写配置

### 发言轮次检测

- `type`: 检测方法（`server_vad`、`semantic_vad`）
- `threshold`: 语音活动阈值（0.0-1.0）
- `silence_duration_ms`: 用于检测轮次结束的静音时长
- `prefix_padding_ms`: 语音前的音频填充

## 后续步骤

- [进一步了解实时智能体](guide.md)
- 查看 [examples/realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) 文件夹中的可运行的 code examples
- 为你的智能体添加工具
- 实现智能体之间的任务转移
- 设置安全防护措施以确保安全

## 身份验证

请确保你的 OpenAI API key 已在环境中设置：

```bash
export OPENAI_API_KEY="your-api-key-here"
```

或在创建会话时直接传入：

```python
session = await runner.run(model_config={"api_key": "your-api-key"})
```