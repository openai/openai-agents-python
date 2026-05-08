---
search:
  exclude: true
---
# リアルタイムエージェントガイド

このガイドでは、OpenAI Agents SDK のリアルタイムレイヤーが OpenAI Realtime API にどのように対応するか、また Python SDK がその上に追加する挙動について説明します。

!!! warning "ベータ機能"

    リアルタイムエージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する可能性があります。

!!! note "最初に読むもの"

    デフォルトの Python パスを使いたい場合は、まず [クイックスタート](quickstart.md) を読んでください。アプリでサーバーサイド WebSocket と SIP のどちらを使うべきかを検討している場合は、[リアルタイムトランスポート](transport.md) を読んでください。ブラウザーの WebRTC トランスポートは Python SDK の一部ではありません。

## 概要

リアルタイムエージェントは Realtime API への長時間接続を維持し、モデルがテキストと音声を段階的に処理し、音声出力をストリーミングし、ツールを呼び出し、各ターンで新しいリクエストを再開することなく割り込みを処理できるようにします。

主な SDK コンポーネントは次のとおりです。

-   **RealtimeAgent**: 1 つのリアルタイム専門エージェント向けの instructions、ツール、出力ガードレール、ハンドオフ
-   **RealtimeRunner**: 開始エージェントをリアルタイムトランスポートに接続するセッションファクトリー
-   **RealtimeSession**: 入力を送信し、イベントを受信し、履歴を追跡し、ツールを実行するライブセッション
-   **RealtimeModel**: トランスポート抽象化です。デフォルトは OpenAI のサーバーサイド WebSocket 実装です。

## セッションライフサイクル

典型的なリアルタイムセッションは次のようになります。

1. 1 つ以上の `RealtimeAgent` を作成します。
2. 開始エージェントで `RealtimeRunner` を作成します。
3. `await runner.run()` を呼び出して `RealtimeSession` を取得します。
4. `async with session:` または `await session.enter()` でセッションに入ります。
5. `send_message()` または `send_audio()` でユーザー入力を送信します。
6. 会話が終了するまでセッションイベントを反復処理します。

テキストのみの実行とは異なり、`runner.run()` は最終的な実行結果をすぐには生成しません。ローカル履歴、バックグラウンドのツール実行、ガードレール状態、アクティブなエージェント設定をトランスポート層と同期し続けるライブセッションオブジェクトを返します。

デフォルトでは、`RealtimeRunner` は `OpenAIRealtimeWebSocketModel` を使用するため、デフォルトの Python パスは Realtime API へのサーバーサイド WebSocket 接続です。別の `RealtimeModel` を渡した場合でも、同じセッションライフサイクルとエージェント機能が適用されますが、接続の仕組みは変わる場合があります。

## エージェントとセッション設定

`RealtimeAgent` は、通常の `Agent` 型より意図的に範囲が狭くなっています。

-   モデル選択はエージェントごとではなく、セッションレベルで設定します。
-   structured outputs はサポートされていません。
-   音声は設定できますが、セッションがすでに発話音声を生成した後に変更することはできません。
-   instructions、関数ツール、ハンドオフ、フック、出力ガードレールはすべて引き続き機能します。

`RealtimeSessionModelSettings` は、新しいネストされた `audio` 設定と古いフラットなエイリアスの両方をサポートします。新しいコードではネストされた形を優先し、新しいリアルタイムエージェントでは `gpt-realtime-2` から始めてください。

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
                    "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
                },
                "output": {"format": "pcm16", "voice": "ash"},
            },
            "tool_choice": "auto",
        }
    },
)
```

便利なセッションレベル設定には次のものがあります。

-   `audio.input.format`, `audio.output.format`
-   `audio.input.transcription`
-   `audio.input.noise_reduction`
-   `audio.input.turn_detection`
-   `audio.output.voice`, `audio.output.speed`
-   `output_modalities`
-   `tool_choice`
-   `prompt`
-   `tracing`

`RealtimeRunner(config=...)` の便利な実行レベル設定には次のものがあります。

-   `async_tool_calls`
-   `output_guardrails`
-   `guardrails_settings.debounce_text_length`
-   `tool_error_formatter`
-   `tracing_disabled`

型付きの全体仕様については、[`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] と [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings] を参照してください。

## 入力と出力

### テキストと構造化されたユーザーメッセージ

プレーンテキストまたは構造化されたリアルタイムメッセージには [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用します。

```python
from agents.realtime import RealtimeUserInputMessage

await session.send_message("Summarize what we discussed so far.")

message: RealtimeUserInputMessage = {
    "type": "message",
    "role": "user",
    "content": [
        {"type": "input_text", "text": "Describe this image."},
        {"type": "input_image", "image_url": image_data_url, "detail": "high"},
    ],
}
await session.send_message(message)
```

構造化メッセージは、リアルタイム会話に画像入力を含める主な方法です。[`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) の Web デモ例は、この方法で `input_image` メッセージを転送します。

### 音声入力

raw 音声バイトをストリーミングするには [`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio] を使用します。

```python
await session.send_audio(audio_bytes)
```

サーバーサイドのターン検出が無効になっている場合は、ターン境界を示す責任があります。高レベルの便利な方法は次のとおりです。

```python
await session.send_audio(audio_bytes, commit=True)
```

より低レベルの制御が必要な場合は、基盤となるモデル トランスポートを通じて `input_audio_buffer.commit` などの raw クライアントイベントを送信することもできます。

### 手動レスポンス制御

`session.send_message()` は高レベルのパスを使ってユーザー入力を送信し、レスポンスを開始します。raw 音声バッファリングは、すべての設定で **自動的に** 同じことを行うわけではありません。

Realtime API レベルでは、手動ターン制御とは raw `session.update` で `turn_detection` をクリアし、その後 `input_audio_buffer.commit` と `response.create` を自分で送信することを意味します。

ターンを手動で管理している場合は、モデル トランスポートを通じて raw クライアントイベントを送信できます。

```python
from agents.realtime.model_inputs import RealtimeModelSendRawMessage

await session.model.send_event(
    RealtimeModelSendRawMessage(
        message={
            "type": "response.create",
        }
    )
)
```

このパターンは次の場合に便利です。

-   `turn_detection` が無効で、モデルがいつ応答すべきかを自分で決めたい場合
-   レスポンスをトリガーする前にユーザー入力を検査またはゲートしたい場合
-   アウトオブバンドレスポンス用にカスタムプロンプトが必要な場合

[`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) の SIP 例では、開始時のあいさつを強制するために raw `response.create` を使用しています。

## イベント、履歴、割り込み

`RealtimeSession` は、必要に応じて raw モデルイベントも転送しながら、より高レベルの SDK イベントを発行します。

価値の高いセッションイベントには次のものがあります。

-   `audio`, `audio_end`, `audio_interrupted`
-   `agent_start`, `agent_end`
-   `tool_start`, `tool_end`, `tool_approval_required`
-   `handoff`
-   `history_added`, `history_updated`
-   `guardrail_tripped`
-   `input_audio_timeout_triggered`
-   `error`
-   `raw_model_event`

UI 状態に最も役立つイベントは通常、`history_added` と `history_updated` です。これらは、ユーザーメッセージ、アシスタントメッセージ、ツール呼び出しを含む `RealtimeItem` オブジェクトとして、セッションのローカル履歴を公開します。

### 割り込みと再生トラッキング

ユーザーがアシスタントに割り込むと、セッションは `audio_interrupted` を発行し、サーバーサイドの会話がユーザーが実際に聞いた内容と一致するように履歴を更新します。

低レイテンシのローカル再生では、デフォルトの再生トラッカーで十分なことが多いです。リモート再生や遅延再生のシナリオ、特に電話では、生成された音声がすべてすでに聞かれたと仮定するのではなく、実際の再生進行に基づいて割り込み時の切り詰めが行われるように [`RealtimePlaybackTracker`][agents.realtime.model.RealtimePlaybackTracker] を使用してください。

[`examples/realtime/twilio/twilio_handler.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio/twilio_handler.py) の Twilio 例は、このパターンを示しています。

## ツール、承認、ハンドオフ、ガードレール

### 関数ツール

リアルタイムエージェントは、ライブ会話中に関数ツールをサポートします。

```python
from agents import function_tool


@function_tool
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    return f"The weather in {city} is sunny, 72F."


agent = RealtimeAgent(
    name="Assistant",
    instructions="You can answer weather questions.",
    tools=[get_weather],
)
```

### ツール承認

関数ツールは、実行前に人間の承認を必要とする場合があります。その場合、セッションは `tool_approval_required` を発行し、`approve_tool_call()` または `reject_tool_call()` を呼び出すまでツール実行を一時停止します。

```python
async for event in session:
    if event.type == "tool_approval_required":
        await session.approve_tool_call(event.call_id)
```

具体的なサーバーサイドの承認ループについては、[`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) を参照してください。Human-in-the-loop のドキュメントも、[Human in the loop](../human_in_the_loop.md) でこのフローを参照しています。

### ハンドオフ

リアルタイムハンドオフにより、あるエージェントがライブ会話を別の専門エージェントに転送できます。

```python
from agents.realtime import RealtimeAgent, realtime_handoff

billing_agent = RealtimeAgent(
    name="Billing Support",
    instructions="You specialize in billing issues.",
)

main_agent = RealtimeAgent(
    name="Customer Service",
    instructions="Triage the request and hand off when needed.",
    handoffs=[realtime_handoff(billing_agent, tool_description="Transfer to billing support")],
)
```

素の `RealtimeAgent` ハンドオフは自動的にラップされ、`realtime_handoff(...)` を使うと名前、説明、検証、コールバック、可用性をカスタマイズできます。リアルタイムハンドオフは、通常のハンドオフ `input_filter` をサポートして **いません**。

### ガードレール

リアルタイムエージェントでは出力ガードレールのみがサポートされています。これらは部分トークンごとではなく、デバウンスされたトランスクリプト蓄積に対して実行され、例外を発生させる代わりに `guardrail_tripped` を発行します。

```python
from agents.guardrail import GuardrailFunctionOutput, OutputGuardrail


def sensitive_data_check(context, agent, output):
    return GuardrailFunctionOutput(
        tripwire_triggered="password" in output,
        output_info=None,
    )


agent = RealtimeAgent(
    name="Assistant",
    instructions="...",
    output_guardrails=[OutputGuardrail(guardrail_function=sensitive_data_check)],
)
```

リアルタイム出力ガードレールが作動すると、セッションはアクティブなレスポンスに割り込み、
`response.cancel` を強制し、`guardrail_tripped` を発行し、トリガーされたガードレールの名前を含むフォローアップのユーザーメッセージを送信して、モデルが代替レスポンスを生成できるようにします。音声プレーヤーは引き続き
`audio_interrupted` をリッスンし、ローカル再生をただちに停止する必要があります。これは、ガードレールがデバウンスされたトランスクリプトテキストに対して実行され、トリップワイヤーが発火した時点で一部の音声がすでにバッファリングされている可能性があるためです。

## SIP と電話

Python SDK には、[`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] によるファーストクラスの SIP アタッチフローが含まれています。

Realtime Calls API 経由で通話が到着し、結果として得られる `call_id` にエージェントセッションをアタッチしたい場合に使用します。

```python
from agents.realtime import RealtimeRunner
from agents.realtime.openai_realtime import OpenAIRealtimeSIPModel

runner = RealtimeRunner(starting_agent=agent, model=OpenAIRealtimeSIPModel())

async with await runner.run(
    model_config={
        "call_id": call_id_from_webhook,
    }
) as session:
    async for event in session:
        ...
```

最初に通話を受け入れる必要があり、受け入れペイロードをエージェント由来のセッション設定と一致させたい場合は、`OpenAIRealtimeSIPModel.build_initial_session_payload(...)` を使用してください。完全なフローは [`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) に示されています。

## 低レベルアクセスとカスタムエンドポイント

`session.model` を通じて基盤となるトランスポートオブジェクトにアクセスできます。

これは次の場合に使用します。

-   `session.model.add_listener(...)` によるカスタムリスナー
-   `response.create` や `session.update` などの raw クライアントイベント
-   `model_config` を通じたカスタム `url`、`headers`、または `api_key` の処理
-   既存のリアルタイム通話への `call_id` アタッチ

`RealtimeModelConfig` は次をサポートします。

-   `api_key`
-   `url`
-   `headers`
-   `initial_model_settings`
-   `playback_tracker`
-   `call_id`

このリポジトリに同梱されている `call_id` 例は SIP です。より広範な Realtime API でも、一部のサーバーサイド制御フローで `call_id` を使用しますが、それらはここでは Python 例としてパッケージ化されていません。

Azure OpenAI に接続する場合は、GA Realtime エンドポイント URL と明示的なヘッダーを渡します。例:

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"api-key": "<your-azure-api-key>"},
    }
)
```

トークンベース認証では、`headers` に bearer トークンを使用します。

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"authorization": f"Bearer {token}"},
    }
)
```

`headers` を渡した場合、SDK は `Authorization` を自動的に追加しません。リアルタイムエージェントでは、レガシーのベータパス (`/openai/realtime?api-version=...`) を避けてください。

## 関連資料

-   [リアルタイムトランスポート](transport.md)
-   [クイックスタート](quickstart.md)
-   [OpenAI Realtime 会話](https://developers.openai.com/api/docs/guides/realtime-conversations/)
-   [OpenAI Realtime サーバーサイド制御](https://developers.openai.com/api/docs/guides/realtime-server-controls/)
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)