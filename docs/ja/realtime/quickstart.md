---
search:
  exclude: true
---
# クイックスタート

Realtime エージェントを使用すると、OpenAI の Realtime API を介して AI エージェントと音声会話が行えます。ここでは、最初の realtime 音声エージェントを作成する方法を説明します。

!!! warning "Beta feature"
Realtime エージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する可能性があります。

## 前提条件

-   Python 3.9 以上
-   OpenAI API キー
-   OpenAI Agents SDK の基本的な知識

## インストール

まだインストールしていない場合は、OpenAI Agents SDK をインストールしてください：

```bash
pip install openai-agents
```

## 初めての realtime エージェント作成

### 1. 必要なコンポーネントをインポート

```python
import asyncio
from agents.realtime import RealtimeAgent, RealtimeRunner
```

### 2. realtime エージェントを作成

```python
agent = RealtimeAgent(
    name="Assistant",
    instructions="You are a helpful voice assistant. Keep your responses conversational and friendly.",
)
```

### 3. runner を設定

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "model_name": "gpt-4o-realtime-preview",
            "voice": "alloy",
            "modalities": ["text", "audio"],
        }
    }
)
```

### 4. セッションを開始

```python
async def main():
    # Start the realtime session
    session = await runner.run()

    async with session:
        # Send a text message to start the conversation
        await session.send_message("Hello! How are you today?")

        # The agent will stream back audio in real-time (not shown in this example)
        # Listen for events from the session
        async for event in session:
            if event.type == "response.audio_transcript.done":
                print(f"Assistant: {event.transcript}")
            elif event.type == "conversation.item.input_audio_transcription.completed":
                print(f"User: {event.transcript}")

# Run the session
asyncio.run(main())
```

## 完全な例

以下は動作する完全な例です：

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
                "model_name": "gpt-4o-realtime-preview",
                "voice": "alloy",
                "modalities": ["text", "audio"],
                "input_audio_transcription": {
                    "model": "whisper-1"
                },
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 200
                }
            }
        }
    )

    # Start the session
    session = await runner.run()

    async with session:
        print("Session started! The agent will stream audio responses in real-time.")

        # Process events
        async for event in session:
            if event.type == "response.audio_transcript.done":
                print(f"Assistant: {event.transcript}")
            elif event.type == "conversation.item.input_audio_transcription.completed":
                print(f"User: {event.transcript}")
            elif event.type == "error":
                print(f"Error: {event.error}")
                break

if __name__ == "__main__":
    asyncio.run(main())
```

## 設定オプション

### モデル設定

-   `model_name`: 利用可能な realtime モデルから選択（例：`gpt-4o-realtime-preview`）
-   `voice`: 音声を選択（`alloy`, `echo`, `fable`, `onyx`, `nova`, `shimmer`）
-   `modalities`: テキストおよび／または音声を有効化（`["text", "audio"]`）

### オーディオ設定

-   `input_audio_format`: 入力音声のフォーマット（`pcm16`, `g711_ulaw`, `g711_alaw`）
-   `output_audio_format`: 出力音声のフォーマット
-   `input_audio_transcription`: 音声認識の設定

### ターン検出

-   `type`: 検出方法（`server_vad`, `semantic_vad`）
-   `threshold`: 音声アクティビティ閾値（0.0–1.0）
-   `silence_duration_ms`: ターン終了を検出する無音時間
-   `prefix_padding_ms`: 発話前の音声パディング

## 次のステップ

-   [realtime エージェントについて詳しく学ぶ](guide.md)
-   [examples/realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) フォルダーの動作する code examples を確認
-   ツールをエージェントに追加
-   エージェント間のハンドオフを実装
-   安全のためのガードレールを設定

## 認証

OpenAI API キーを環境変数に設定してください：

```bash
export OPENAI_API_KEY="your-api-key-here"
```

またはセッション作成時に直接渡してください：

```python
session = await runner.run(model_config={"api_key": "your-api-key"})
```