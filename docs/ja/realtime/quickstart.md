---
search:
  exclude: true
---
# クイックスタート

リアルタイム エージェントは、 OpenAI の Realtime API を使って AI エージェントとの音声会話を可能にします。本ガイドでは、最初のリアルタイム音声エージェントの作成手順を説明します。

!!! warning "ベータ機能"
リアルタイム エージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する場合があります。

## 前提条件

- Python 3.9 以上
- OpenAI API キー
- OpenAI Agents SDK の基本的な知識

## インストール

まだの場合は、 OpenAI Agents SDK をインストールします:

```bash
pip install openai-agents
```

## 最初のリアルタイム エージェントの作成

### 1. 必要なコンポーネントのインポート

```python
import asyncio
from agents.realtime import RealtimeAgent, RealtimeRunner
```

### 2. リアルタイム エージェントの作成

```python
agent = RealtimeAgent(
    name="Assistant",
    instructions="You are a helpful voice assistant. Keep your responses conversational and friendly.",
)
```

### 3. ランナーのセットアップ

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

### 4. セッションの開始

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

## 完全なコード例

完全に動作するコード例は次のとおりです:

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

## 構成オプション

### モデル設定

- `model_name`: 利用可能なリアルタイム モデルから選択 (例: `gpt-realtime`)
- `voice`: 音声の選択 (`alloy`, `echo`, `fable`, `onyx`, `nova`, `shimmer`)
- `modalities`: テキストまたは音声を有効化 (`["text"]` または `["audio"]`)

### オーディオ設定

- `input_audio_format`: 入力オーディオの形式 (`pcm16`, `g711_ulaw`, `g711_alaw`)
- `output_audio_format`: 出力オーディオの形式
- `input_audio_transcription`: 文字起こしの構成

### ターン検出

- `type`: 検出方法 (`server_vad`, `semantic_vad`)
- `threshold`: 音声活動の閾値 ( 0.0-1.0 )
- `silence_duration_ms`: ターン終了検出のための無音継続時間
- `prefix_padding_ms`: 発話前のオーディオ パディング

## 次のステップ

- [リアルタイム エージェントについてさらに学ぶ](guide.md)
- 動作するサンプルコードは [examples/realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) フォルダにあります
- エージェントにツールを追加
- エージェント間のハンドオフを実装
- 安全のためのガードレールを設定

## 認証

OpenAI API キーが環境に設定されていることを確認してください:

```bash
export OPENAI_API_KEY="your-api-key-here"
```

またはセッション作成時に直接渡します:

```python
session = await runner.run(model_config={"api_key": "your-api-key"})
```