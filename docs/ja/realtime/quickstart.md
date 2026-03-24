---
search:
  exclude: true
---
# クイックスタート

Python SDK の Realtime エージェントは、WebSocket トランスポート経由の OpenAI Realtime API 上に構築された、サーバーサイドの低レイテンシなエージェントです。

!!! warning "Beta 機能"

    Realtime エージェントは beta です。実装の改善に伴い、破壊的変更が発生する可能性があります。

!!! note "Python SDK の範囲"

    Python SDK はブラウザー向けの WebRTC トランスポートを **提供しません** 。このページでは、サーバーサイド WebSocket 経由で Python が管理する realtime session のみを扱います。サーバーサイドのオーケストレーション、ツール、承認、テレフォニー統合にはこの SDK を使用してください。あわせて [Realtime transport](transport.md) も参照してください。

## 前提条件

-   Python 3.10 以上
-   OpenAI API キー
-   OpenAI Agents SDK の基本的な理解

## インストール

まだの場合は、OpenAI Agents SDK をインストールします。

```bash
pip install openai-agents
```

## サーバーサイド realtime session の作成

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

新しいコードでは、ネストされた `audio.input` / `audio.output` session 設定の形式を推奨します。新しい Realtime エージェントでは、`gpt-realtime-1.5` から始めてください。

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

### 4. session の開始と入力の送信

`runner.run()` は `RealtimeSession` を返します。session context に入ると接続が開かれます。

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

`session.send_message()` はプレーンな文字列または構造化された realtime message のいずれかを受け取ります。raw audio chunk には [`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio] を使用してください。

## このクイックスタートに含まれない内容

-   マイク入力とスピーカー再生のコード。[`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) の realtime コード例を参照してください。
-   SIP / テレフォニー接続フロー。[Realtime transport](transport.md) と [SIP セクション](guide.md#sip-and-telephony) を参照してください。

## 主要設定

基本的な session が動作したら、次によく使われる設定は以下です。

-   `model_name`
-   `audio.input.format`, `audio.output.format`
-   `audio.input.transcription`
-   `audio.input.noise_reduction`
-   自動ターン検出のための `audio.input.turn_detection`
-   `audio.output.voice`
-   `tool_choice`, `prompt`, `tracing`
-   `async_tool_calls`, `guardrails_settings.debounce_text_length`, `tool_error_formatter`

`input_audio_format`、`output_audio_format`、`input_audio_transcription`、`turn_detection` などの古いフラットな別名も引き続き動作しますが、新しいコードではネストされた `audio` 設定を推奨します。

手動でターン制御を行う場合は、[Realtime agents guide](guide.md#manual-response-control) にある説明のとおり、raw の `session.update` / `input_audio_buffer.commit` / `response.create` フローを使用してください。

完全なスキーマについては、[`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] と [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings] を参照してください。

## 接続オプション

環境変数に API キーを設定します。

```bash
export OPENAI_API_KEY="your-api-key-here"
```

または、session 開始時に直接渡します。

```python
session = await runner.run(model_config={"api_key": "your-api-key"})
```

`model_config` は次もサポートします。

-   `url`: カスタム WebSocket endpoint
-   `headers`: カスタム request header
-   `call_id`: 既存の realtime call に接続します。このリポジトリで文書化されている接続フローは SIP です。
-   `playback_tracker`: ユーザーが実際に聞いた audio の量を報告します

`headers` を明示的に渡した場合、SDK は `Authorization` header を **自動挿入しません** 。

Azure OpenAI に接続する場合は、`model_config["url"]` に GA Realtime endpoint URL と明示的な headers を渡してください。realtime エージェントでは、legacy beta path (`/openai/realtime?api-version=...`) を避けてください。詳細は [Realtime agents guide](guide.md#low-level-access-and-custom-endpoints) を参照してください。

## 次のステップ

-   サーバーサイド WebSocket と SIP のどちらを選ぶか判断するために [Realtime transport](transport.md) を読んでください。
-   ライフサイクル、構造化入力、承認、ハンドオフ、ガードレール、低レベル制御について [Realtime agents guide](guide.md) を読んでください。
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) のコード例を確認してください。