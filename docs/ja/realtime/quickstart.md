---
search:
  exclude: true
---
# クイックスタート

Python SDK のリアルタイムエージェントは、WebSocket トランスポート経由の OpenAI Realtime APIを基盤とする、サーバー側で動作する低レイテンシーのエージェントです。

!!! note "Python SDK の境界"

    Python SDK は、ブラウザー向け WebRTC トランスポートを **提供しません** 。このページでは、サーバー側の WebSocket を介して Python で管理されるリアルタイムセッションのみを扱います。この SDK は、サーバー側のオーケストレーション、ツール、承認、テレフォニー統合に使用してください。[リアルタイムトランスポート](transport.md)も参照してください。

## 前提条件 {#prerequisites}

-   Python 3.10 以降
-   OpenAI API キー
-   OpenAI Agents SDKの基本的な知識

## インストール {#installation}

まだインストールしていない場合は、OpenAI Agents SDKをインストールします。

```bash
pip install openai-agents
```

## サーバー側リアルタイムセッションの作成 {#create-a-server-side-realtime-session}

### 1. リアルタイムコンポーネントのインポート {#1-import-the-realtime-components}

```python
import asyncio

from agents.realtime import RealtimeAgent, RealtimeRunner
```

### 2. 開始エージェントの定義 {#2-define-the-starting-agent}

```python
agent = RealtimeAgent(
    name="Assistant",
    instructions="You are a helpful voice assistant. Keep responses short and conversational.",
)
```

### 3. ランナーの設定 {#3-configure-the-runner}

新しいコードでは、ネストされた `audio.input` / `audio.output` セッション設定形式を推奨します。新しいリアルタイムエージェントでは、`gpt-realtime-2.1` から始めてください。

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "model_name": "gpt-realtime-2.1",
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

### 4. セッションの開始と入力の送信 {#4-start-the-session-and-send-input}

`runner.run()` は `RealtimeSession` を返します。セッションコンテキストに入ると、接続が開かれます。

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

`session.send_message()` は、プレーン文字列または構造化されたリアルタイムメッセージを受け付けます。raw オーディオチャンクには、[`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio] を使用してください。

## 本クイックスタートの対象外 {#what-this-quickstart-does-not-include}

-   マイク入力とスピーカー再生のコード。[`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) のリアルタイムコード例を参照してください。
-   SIP / テレフォニーの接続フロー。[リアルタイムトランスポート](transport.md)および [SIP セクション](guide.md#sip-and-telephony)を参照してください。

## 主要な設定 {#key-settings}

基本的なセッションが動作した後、多くの場合に次に使用される設定は以下のとおりです。

-   `model_name`
-   `audio.input.format`, `audio.output.format`
-   `audio.input.transcription`
-   `audio.input.noise_reduction`
-   自動ターン検出用の `audio.input.turn_detection`
-   `audio.output.voice`
-   `tool_choice`, `prompt`, `tracing`
-   `async_tool_calls`, `tool_execution.pre_approval_tool_input_guardrails`, `guardrails_settings.debounce_text_length`, `tool_error_formatter`

`input_audio_format`、`output_audio_format`、`input_audio_transcription`、`turn_detection` などの従来のフラットなエイリアスも引き続き機能しますが、新しいコードではネストされた `audio` 設定を推奨します。

ターンを手動で制御するには、[リアルタイムエージェントガイド](guide.md#manual-response-control)で説明されている低レベルの `session.update` / `input_audio_buffer.commit` / `response.create` フローを使用してください。

完全なスキーマについては、[`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] および [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings] を参照してください。

## 接続オプション {#connection-options}

環境変数に API キーを設定します。

```bash
export OPENAI_API_KEY="your-api-key-here"
```

または、セッションの開始時に直接渡します。

```python
session = await runner.run(model_config={"api_key": "your-api-key"})
```

`model_config` は、以下もサポートしています。

-   `url`: カスタム WebSocket エンドポイント
-   `headers`: カスタムリクエストヘッダー
-   `call_id`: 既存のリアルタイム通話への接続。このリポジトリで文書化されている接続フローは SIP です。
-   `playback_tracker`: ユーザーが実際に聞いたオーディオ量の報告

`headers` を明示的に渡した場合、SDK は `Authorization` ヘッダーを自動的に **挿入しません** 。

Azure OpenAIに接続する場合は、`model_config["url"]` を GA 版 Realtime エンドポイント URL に設定し、ヘッダーを明示的に渡してください。リアルタイムエージェントでは、従来のベータ版パス（`/openai/realtime?api-version=...`）を避けてください。詳細については、[リアルタイムエージェントガイド](guide.md#low-level-access-and-custom-endpoints)を参照してください。

## 次のステップ {#next-steps}

-   サーバー側 WebSocket と SIP のどちらを使用するか選択するには、[リアルタイムトランスポート](transport.md)をお読みください。
-   ライフサイクル、構造化入力、承認、ハンドオフ、ガードレール、低レベル制御については、[リアルタイムエージェントガイド](guide.md)をお読みください。
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) のコード例を参照してください。