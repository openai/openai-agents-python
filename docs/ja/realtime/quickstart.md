---
search:
  exclude: true
---
# クイックスタート

Realtime エージェントを使用すると、OpenAI の Realtime API を介して AI エージェントとの音声会話が可能になります。このガイドでは、初めての Realtime 音声エージェントを作成する手順を説明します。

!!! warning "Beta feature"
Beta 機能  
Realtime エージェントはベータ版です。実装を改善する過程で非互換の変更が入る可能性があります。

## 前提条件

-   Python 3.9 以上  
-   OpenAI API キー  
-   OpenAI Agents SDK に関する基本的な知識  

## インストール

まだインストールしていない場合は、OpenAI Agents SDK をインストールします:

```bash
pip install openai-agents
```

## 最初の Realtime エージェントの作成

### 1. 必要なコンポーネントをインポート

```python
import asyncio
from agents.realtime import RealtimeAgent, RealtimeRunner
```

### 2. Realtime エージェントを作成

```python
agent = RealtimeAgent(
    name="Assistant",
    instructions="You are a helpful voice assistant. Keep your responses conversational and friendly.",
)
```

### 3. Runner をセットアップ

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

## 完全なコード例

以下は動作する完全なコード例です:

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

-   `model_name`: 利用可能な Realtime モデルから選択します (例: `gpt-4o-realtime-preview`)
-   `voice`: 音声を選択します (`alloy`, `echo`, `fable`, `onyx`, `nova`, `shimmer`)
-   `modalities`: テキストおよび／またはオーディオを有効化します (`["text", "audio"]`)

### オーディオ設定

-   `input_audio_format`: 入力オーディオのフォーマット (`pcm16`, `g711_ulaw`, `g711_alaw`)
-   `output_audio_format`: 出力オーディオのフォーマット
-   `input_audio_transcription`: 文字起こしの設定

### ターン検出

-   `type`: 検出方法 (`server_vad`, `semantic_vad`)
-   `threshold`: 音声アクティビティのしきい値 (0.0-1.0)
-   `silence_duration_ms`: ターン終了を検出するための無音時間
-   `prefix_padding_ms`: 発話前のオーディオパディング

## 次のステップ

-   [Realtime エージェントについてさらに学ぶ](guide.md)
-   [examples/realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) フォルダーにある動作するコード例を確認してください
-   エージェントにツールを追加する
-   エージェント間のハンドオフを実装する
-   安全のためのガードレールを設定する

## 認証

OpenAI API キーが環境変数に設定されていることを確認してください:

```bash
export OPENAI_API_KEY="your-api-key-here"
```

あるいは、セッション作成時に直接渡します:

```python
session = await runner.run(model_config={"api_key": "your-api-key"})
```