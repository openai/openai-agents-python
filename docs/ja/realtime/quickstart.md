---
search:
  exclude: true
---
# クイックスタート

Python SDK のリアルタイムエージェントは、WebSocket トランスポート経由の OpenAI Realtime API 上に構築された、サーバー側の低レイテンシーエージェントです。

!!! note "Python SDK の境界"

    Python SDK は、ブラウザーの WebRTC トランスポートを **提供していません** 。このページでは、サーバー側 WebSocket を介して Python で管理されるリアルタイムセッションのみを扱います。この SDK は、サーバー側のオーケストレーション、ツール、承認、テレフォニー連携に使用してください。併せて [リアルタイムトランスポート](transport.md) も参照してください。

## 前提条件

-   Python 3.10 以上
-   OpenAI API キー
-   OpenAI Agents SDK の基本的な知識

## インストール

まだインストールしていない場合は、OpenAI Agents SDK をインストールしてください:

```bash
pip install openai-agents
```

## サーバー側リアルタイムセッションの作成

### 1. リアルタイムコンポーネントのインポート

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

### 3. ランナーの設定

新しいコードでは、ネストされた `audio.input` / `audio.output` のセッション設定形式を推奨します。新しいリアルタイムエージェントでは、`gpt-realtime-2` から始めてください。

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

`session.send_message()` は、プレーン文字列または構造化されたリアルタイムメッセージのいずれかを受け取ります。生の音声チャンクには、[`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio] を使用してください。

## このクイックスタートに含まれない内容

-   マイクのキャプチャおよびスピーカー再生のコード。[`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) のリアルタイムのコード例を参照してください。
-   SIP / テレフォニーのアタッチフロー。[リアルタイムトランスポート](transport.md) と [SIP セクション](guide.md#sip-and-telephony) を参照してください。

## 主要設定

基本的なセッションが動作するようになったら、多くの方が次に利用する設定は次のとおりです:

-   `model_name`
-   `audio.input.format`, `audio.output.format`
-   `audio.input.transcription`
-   `audio.input.noise_reduction`
-   `audio.input.turn_detection`（自動ターン検出用）
-   `audio.output.voice`
-   `tool_choice`, `prompt`, `tracing`
-   `async_tool_calls`, `tool_execution.pre_approval_tool_input_guardrails`, `guardrails_settings.debounce_text_length`, `tool_error_formatter`

`input_audio_format`、`output_audio_format`、`input_audio_transcription`、`turn_detection` などの古いフラットなエイリアスも引き続き機能しますが、新しいコードではネストされた `audio` 設定が推奨されます。

手動のターン制御には、[リアルタイムエージェントガイド](guide.md#manual-response-control) で説明されている raw な `session.update` / `input_audio_buffer.commit` / `response.create` フローを使用してください。

完全なスキーマについては、[`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] および [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings] を参照してください。

## 接続オプション

API キーを環境変数に設定してください:

```bash
export OPENAI_API_KEY="your-api-key-here"
```

または、セッションの開始時に直接渡します:

```python
session = await runner.run(model_config={"api_key": "your-api-key"})
```

`model_config` は以下にも対応しています:

-   `url`: カスタム WebSocket エンドポイント
-   `headers`: カスタムリクエストヘッダー
-   `call_id`: 既存のリアルタイムコールにアタッチします。このリポジトリでドキュメント化されているアタッチフローは SIP です。
-   `playback_tracker`: ユーザーが実際に聞いた音声量を報告します

`headers` を明示的に渡す場合、SDK は `Authorization` ヘッダーを **挿入しません** 。

Azure OpenAI に接続する場合は、`model_config["url"]` に GA Realtime エンドポイント URL を指定し、明示的なヘッダーも渡してください。リアルタイムエージェントでは、レガシーな beta パス（`/openai/realtime?api-version=...`）の使用は避けてください。詳細については、[リアルタイムエージェントガイド](guide.md#low-level-access-and-custom-endpoints) を参照してください。

## 次のステップ

-   サーバー側 WebSocket と SIP のどちらを選ぶかを判断するには、[リアルタイムトランスポート](transport.md) をお読みください。
-   ライフサイクル、構造化入力、承認、ハンドオフ、ガードレール、低レベル制御については、[リアルタイムエージェントガイド](guide.md) をお読みください。
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) のコード例をご覧ください。