---
search:
  exclude: true
---
# クイックスタート

Realtime エージェントを使用すると、OpenAI の Realtime API を使って AI エージェントと音声会話ができます。このガイドでは、最初の realtime 音声エージェントを作成する手順を説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装を改善する過程で、破壊的変更が入る可能性があります。

## 前提条件

-   Python 3.9 以上
-   OpenAI API キー
-   OpenAI Agents SDK の基本的な理解

## インストール

まだの場合は、OpenAI Agents SDK をインストールしてください。

```bash
pip install openai-agents
```

## 最初の realtime エージェントの作成

### 1. 必要なコンポーネントのインポート

```python
import asyncio
from agents.realtime import RealtimeAgent, RealtimeRunner
```

### 2. realtime エージェントの作成

```python
agent = RealtimeAgent(
    name="Assistant",
    instructions="You are a helpful voice assistant. Keep your responses conversational and friendly.",
)
```

### 3. Runner の設定

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

## 完全な例

動作する完全な例は次のとおりです。

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

## 設定オプション

### モデル設定

-   `model_name`: 利用可能な realtime モデルから選択します（例: `gpt-realtime`）
-   `voice`: 音声を選択します（`alloy`、`echo`、`fable`、`onyx`、`nova`、`shimmer`）
-   `modalities`: テキストまたは音声を有効化します（`["text"]` または `["audio"]`）

### 音声設定

-   `input_audio_format`: 入力音声の形式（`pcm16`、`g711_ulaw`、`g711_alaw`）
-   `output_audio_format`: 出力音声の形式
-   `input_audio_transcription`: 文字起こしの設定

### ターン検出

-   `type`: 検出方式（`server_vad`、`semantic_vad`）
-   `threshold`: 音声活動のしきい値（0.0-1.0）
-   `silence_duration_ms`: ターン終了を検出する無音時間
-   `prefix_padding_ms`: 発話前の音声パディング

## 次のステップ

-   [realtime エージェントの詳細](guide.md)
-   [examples/realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) フォルダーの動作する例を確認する
-   エージェントにツールを追加する
-   エージェント間のハンドオフを実装する
-   安全性のためのガードレールを設定する

## 認証

環境に OpenAI API キーが設定されていることを確認してください。

```bash
export OPENAI_API_KEY="your-api-key-here"
```

または、セッション作成時に直接渡します。

```python
session = await runner.run(model_config={"api_key": "your-api-key"})
```

## Azure OpenAI エンドポイント形式

OpenAI のデフォルトエンドポイントではなく Azure OpenAI に接続する場合は、`model_config["url"]` に GA Realtime URL を渡し、認証ヘッダーを明示的に設定してください。

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"api-key": "<your-azure-api-key>"},
    }
)
```

ベアラートークンも使用できます。

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"authorization": f"Bearer {token}"},
    }
)
```

realtime エージェントでは、レガシーなベータパス（`/openai/realtime?api-version=...`）の使用は避けてください。SDK は GA Realtime インターフェースを想定しています。