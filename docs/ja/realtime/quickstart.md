---
search:
  exclude: true
---
# クイックスタート

Python SDK の Realtime エージェントは、WebSocket トランスポート上の OpenAI Realtime API を基盤とした、サーバー側の低レイテンシーなエージェントです。

!!! warning "ベータ機能"

    Realtime エージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する可能性があります。

!!! note "Python SDK の範囲"

    Python SDK はブラウザー WebRTC トランスポートを提供 **しません**。このページでは、サーバー側 WebSocket による Python 管理の Realtime セッションのみを扱います。この SDK は、サーバー側のオーケストレーション、ツール、承認、電話連携に使用してください。[Realtime トランスポート](transport.md)も参照してください。

## 前提条件

-   Python 3.10 以上
-   OpenAI API キー
-   OpenAI Agents SDK の基本的な知識

## インストール

まだの場合は、OpenAI Agents SDK をインストールしてください。

```bash
pip install openai-agents
```

## サーバー側 Realtime セッションの作成

### 1. Realtime コンポーネントのインポート

```python
import asyncio

from agents.realtime import RealtimeAgent, RealtimeRunner
```

### 2. 開始エージェントの定義

```python
agent = RealtimeAgent(
    name="Assistant",
    instructions="You are a helpful voice assistant. Keep responses short and conversational.",
)
```

### 3. runner の設定

新しいコードでは、ネストされた `audio.input` / `audio.output` セッション設定形式を使用することを推奨します。新しい Realtime エージェントでは、`gpt-realtime-2` から始めてください。

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

### 4. セッションの開始と入力の送信

`runner.run()` は `RealtimeSession` を返します。セッションコンテキストに入ると接続が開かれます。

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

`session.send_message()` はプレーンな文字列、または構造化された Realtime メッセージのいずれかを受け取ります。raw 音声チャンクには、[`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio] を使用してください。

## このクイックスタートに含まれない内容

-   マイク入力とスピーカー再生のコード。[`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) の Realtime コード例を参照してください。
-   SIP / 電話のアタッチフロー。[Realtime トランスポート](transport.md)および [SIP セクション](guide.md#sip-and-telephony)を参照してください。

## 主要設定

基本的なセッションが動作したら、多くの人が次に扱う設定は次のとおりです。

-   `model_name`
-   `audio.input.format`, `audio.output.format`
-   `audio.input.transcription`
-   `audio.input.noise_reduction`
-   自動ターン検出のための `audio.input.turn_detection`
-   `audio.output.voice`
-   `tool_choice`, `prompt`, `tracing`
-   `async_tool_calls`, `guardrails_settings.debounce_text_length`, `tool_error_formatter`

`input_audio_format`, `output_audio_format`, `input_audio_transcription`, `turn_detection` などの古いフラットなエイリアスも引き続き動作しますが、新しいコードではネストされた `audio` 設定が推奨されます。

手動でターンを制御するには、[Realtime エージェントガイド](guide.md#manual-response-control)で説明されている raw の `session.update` / `input_audio_buffer.commit` / `response.create` フローを使用してください。

完全なスキーマについては、[`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] および [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings] を参照してください。

## 接続オプション

API キーを環境に設定します。

```bash
export OPENAI_API_KEY="your-api-key-here"
```

または、セッション開始時に直接渡します。

```python
session = await runner.run(model_config={"api_key": "your-api-key"})
```

`model_config` は次もサポートします。

-   `url`: カスタム WebSocket エンドポイント
-   `headers`: カスタムリクエストヘッダー
-   `call_id`: 既存の Realtime 呼び出しにアタッチします。このリポジトリでは、文書化されているアタッチフローは SIP です。
-   `playback_tracker`: ユーザーが実際に聞いた音声量を報告します

`headers` を明示的に渡した場合、SDK は `Authorization` ヘッダーを自動で注入 **しません**。

Azure OpenAI に接続する場合は、`model_config["url"]` に GA Realtime エンドポイント URL と明示的なヘッダーを渡してください。Realtime エージェントでは、従来のベータパス（`/openai/realtime?api-version=...`）は避けてください。詳細は [Realtime エージェントガイド](guide.md#low-level-access-and-custom-endpoints)を参照してください。

## 次のステップ

-   [Realtime トランスポート](transport.md)を読み、サーバー側 WebSocket と SIP のどちらを使用するかを選択してください。
-   ライフサイクル、構造化入力、承認、ハンドオフ、ガードレール、低レベル制御については、[Realtime エージェントガイド](guide.md)を読んでください。
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) のコード例を参照してください。