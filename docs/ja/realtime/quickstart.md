---
search:
  exclude: true
---
# クイックスタート

Python SDK のリアルタイムエージェントは、 WebSocket トランスポート経由の OpenAI Realtime API を基盤とする、サーバー側の低レイテンシエージェントです。

!!! warning "ベータ機能"

    リアルタイムエージェントはベータ版です。実装を改善していく中で、破壊的変更が発生する可能性があります。

!!! note "Python SDK の範囲"

    Python SDK はブラウザー向け WebRTC トランスポートを **提供しません** 。このページでは、サーバー側 WebSocket 上で Python が管理するリアルタイムセッションのみを扱います。この SDK は、サーバー側のオーケストレーション、ツール、承認、テレフォニー連携に使用してください。[リアルタイムトランスポート](transport.md) も参照してください。

## 前提条件

-   Python 3.10 以上
-   OpenAI API キー
-   OpenAI Agents SDK に関する基本的な知識

## インストール

まだの場合は、 OpenAI Agents SDK をインストールしてください:

```bash
pip install openai-agents
```

## サーバー側リアルタイムセッションの作成

### 1. リアルタイムコンポーネントのインポート

```python
import asyncio

from agents.realtime import RealtimeAgent, RealtimeRunner
```

### 2. 開始時のエージェントの定義

```python
agent = RealtimeAgent(
    name="Assistant",
    instructions="You are a helpful voice assistant. Keep responses short and conversational.",
)
```

### 3. ランナーの設定

新しいコードでは、ネストされた `audio.input` / `audio.output` のセッション設定形式を推奨します。新しいリアルタイムエージェントでは、 `gpt-realtime-2` から始めてください。

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

`session.send_message()` は、プレーンな文字列または構造化されたリアルタイムメッセージを受け付けます。未加工の音声チャンクには、 [`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio] を使用してください。

## このクイックスタートに含まれない内容

-   マイクキャプチャとスピーカー再生のコード。リアルタイムのコード例については、 [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。
-   SIP / テレフォニーのアタッチフロー。[リアルタイムトランスポート](transport.md) と [SIP セクション](guide.md#sip-and-telephony) を参照してください。

## 主要設定

基本的なセッションが動作したら、多くの方が次に利用する設定は次のとおりです:

-   `model_name`
-   `audio.input.format`, `audio.output.format`
-   `audio.input.transcription`
-   `audio.input.noise_reduction`
-   自動ターン検出用の `audio.input.turn_detection`
-   `audio.output.voice`
-   `tool_choice`, `prompt`, `tracing`
-   `async_tool_calls`, `guardrails_settings.debounce_text_length`, `tool_error_formatter`

`input_audio_format`、`output_audio_format`、`input_audio_transcription`、`turn_detection` などの古いフラットなエイリアスも引き続き機能しますが、新しいコードではネストされた `audio` 設定を推奨します。

手動のターン制御では、 [リアルタイムエージェントガイド](guide.md#manual-response-control) で説明されているように、 raw な `session.update` / `input_audio_buffer.commit` / `response.create` フローを使用してください。

完全なスキーマについては、 [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] と [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings] を参照してください。

## 接続オプション

環境で API キーを設定します:

```bash
export OPENAI_API_KEY="your-api-key-here"
```

または、セッションの開始時に直接渡します:

```python
session = await runner.run(model_config={"api_key": "your-api-key"})
```

`model_config` は以下もサポートしています:

-   `url`: カスタム WebSocket エンドポイント
-   `headers`: カスタムリクエストヘッダー
-   `call_id`: 既存のリアルタイム通話にアタッチします。このリポジトリでは、ドキュメント化されているアタッチフローは SIP です。
-   `playback_tracker`: ユーザーが実際に聞いた音声の量を報告します

`headers` を明示的に渡す場合、 SDK は `Authorization` ヘッダーを **挿入しません** 。

Azure OpenAI に接続する場合は、 `model_config["url"]` に GA Realtime エンドポイント URL を指定し、ヘッダーを明示的に渡してください。リアルタイムエージェントでは、レガシーのベータパス (`/openai/realtime?api-version=...`) は避けてください。詳細は [リアルタイムエージェントガイド](guide.md#low-level-access-and-custom-endpoints) を参照してください。

## 次のステップ

-   サーバー側 WebSocket と SIP のどちらを選ぶかについては、 [リアルタイムトランスポート](transport.md) をお読みください。
-   ライフサイクル、構造化入力、承認、ハンドオフ、ガードレール、低レベル制御については、 [リアルタイムエージェントガイド](guide.md) をお読みください。
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) のコード例を参照してください。