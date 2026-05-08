---
search:
  exclude: true
---
# Realtime エージェントガイド

このガイドでは、OpenAI Agents SDK の realtime レイヤーが OpenAI Realtime API にどのように対応するか、また Python SDK がその上に追加する動作について説明します。

!!! warning "ベータ機能"

    Realtime エージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する可能性があります。

!!! note "ここから開始"

    デフォルトの Python パスを使いたい場合は、まず [クイックスタート](quickstart.md) をお読みください。アプリでサーバーサイド WebSocket と SIP のどちらを使うべきか判断している場合は、[Realtime トランスポート](transport.md) をお読みください。ブラウザー WebRTC トランスポートは Python SDK には含まれていません。

## 概要

Realtime エージェントは、Realtime API への長時間接続を開いたままにすることで、モデルがテキストと音声を段階的に処理し、音声出力をストリーミングし、ツールを呼び出し、各ターンで新しいリクエストを再開始せずに割り込みを処理できるようにします。

主な SDK コンポーネントは次のとおりです。

- **RealtimeAgent**: 1 つの realtime 専門エージェント向けの指示、ツール、出力ガードレール、ハンドオフ
- **RealtimeRunner**: 開始エージェントを realtime トランスポートに接続するセッションファクトリー
- **RealtimeSession**: 入力を送信し、イベントを受信し、履歴を追跡し、ツールを実行するライブセッション
- **RealtimeModel**: トランスポート抽象化です。デフォルトは OpenAI のサーバーサイド WebSocket 実装です。

## セッションライフサイクル

一般的な realtime セッションは次のようになります。

1. 1 つ以上の `RealtimeAgent` を作成します。
2. 開始エージェントを指定して `RealtimeRunner` を作成します。
3. `await runner.run()` を呼び出して `RealtimeSession` を取得します。
4. `async with session:` または `await session.enter()` でセッションに入ります。
5. `send_message()` または `send_audio()` でユーザー入力を送信します。
6. 会話が終了するまでセッションイベントを反復処理します。

テキストのみの実行とは異なり、`runner.run()` は最終結果をすぐには生成しません。ローカル履歴、バックグラウンドのツール実行、ガードレール状態、アクティブなエージェント設定をトランスポートレイヤーと同期し続けるライブセッションオブジェクトを返します。

デフォルトでは、`RealtimeRunner` は `OpenAIRealtimeWebSocketModel` を使用するため、デフォルトの Python パスは Realtime API へのサーバーサイド WebSocket 接続です。別の `RealtimeModel` を渡した場合でも、同じセッションライフサイクルとエージェント機能が適用され、接続の仕組みだけが変わることがあります。

## エージェントとセッション設定

`RealtimeAgent` は通常の `Agent` 型よりも意図的に範囲が狭くなっています。

- モデルの選択は、エージェントごとではなくセッションレベルで設定します。
- Structured outputs はサポートされていません。
- 音声は設定できますが、セッションがすでに発話音声を生成した後には変更できません。
- 指示、関数ツール、ハンドオフ、フック、出力ガードレールはすべて引き続き機能します。

`RealtimeSessionModelSettings` は、新しいネストされた `audio` 設定と古いフラットなエイリアスの両方をサポートしています。新しいコードではネストされた形式を優先し、新しい realtime エージェントでは `gpt-realtime-1.5` から始めてください。

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

- `audio.input.format`, `audio.output.format`
- `audio.input.transcription`
- `audio.input.noise_reduction`
- `audio.input.turn_detection`
- `audio.output.voice`, `audio.output.speed`
- `output_modalities`
- `tool_choice`
- `prompt`
- `tracing`

`RealtimeRunner(config=...)` の便利な実行レベル設定には次のものがあります。

- `async_tool_calls`
- `output_guardrails`
- `guardrails_settings.debounce_text_length`
- `tool_error_formatter`
- `tracing_disabled`

型付きインターフェイス全体については、[`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] と [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings] を参照してください。

## 入力と出力

### テキストと構造化ユーザーメッセージ

プレーンテキストまたは構造化 realtime メッセージには [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用します。

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

構造化メッセージは、realtime 会話に画像入力を含める主な方法です。[`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) の Web デモ例では、この方法で `input_image` メッセージを転送しています。

### 音声入力

raw 音声バイトをストリーミングするには [`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio] を使用します。

```python
await session.send_audio(audio_bytes)
```

サーバーサイドのターン検出が無効な場合は、ターン境界を示す責任があります。高レベルの便利な方法は次のとおりです。

```python
await session.send_audio(audio_bytes, commit=True)
```

より低レベルの制御が必要な場合は、基盤となるモデルトランスポートを通じて `input_audio_buffer.commit` などの raw クライアントイベントを送信することもできます。

### 手動レスポンス制御

`session.send_message()` は高レベルパスを使ってユーザー入力を送信し、レスポンスを開始します。raw 音声バッファリングは、すべての設定で自動的に同じことを行うわけでは**ありません**。

Realtime API レベルでは、手動ターン制御とは、raw `session.update` で `turn_detection` をクリアし、その後 `input_audio_buffer.commit` と `response.create` を自分で送信することを意味します。

ターンを手動で管理している場合は、モデルトランスポートを通じて raw クライアントイベントを送信できます。

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

- `turn_detection` が無効で、モデルがいつ応答すべきかを自分で決めたい場合
- レスポンスをトリガーする前にユーザー入力を検査または制限したい場合
- 帯域外レスポンス用のカスタムプロンプトが必要な場合

[`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) の SIP 例では、開始時のあいさつを強制するために raw `response.create` を使用しています。

## イベント、履歴、割り込み

`RealtimeSession` は、必要に応じて raw モデルイベントを転送しながら、より高レベルの SDK イベントを発行します。

重要なセッションイベントには次のものがあります。

- `audio`, `audio_end`, `audio_interrupted`
- `agent_start`, `agent_end`
- `tool_start`, `tool_end`, `tool_approval_required`
- `handoff`
- `history_added`, `history_updated`
- `guardrail_tripped`
- `input_audio_timeout_triggered`
- `error`
- `raw_model_event`

UI 状態に最も役立つイベントは通常、`history_added` と `history_updated` です。これらは、ユーザーメッセージ、アシスタントメッセージ、ツール呼び出しを含む `RealtimeItem` オブジェクトとして、セッションのローカル履歴を公開します。

### 割り込みと再生追跡

ユーザーがアシスタントを割り込むと、セッションは `audio_interrupted` を発行し、サーバーサイドの会話がユーザーが実際に聞いた内容と一致するように履歴を更新します。

低遅延のローカル再生では、通常、デフォルトの再生トラッカーで十分です。リモートまたは遅延再生のシナリオ、特に電話では、生成された音声がすべてすでに聞かれたと仮定するのではなく、実際の再生進行に基づいて割り込みの切り詰めが行われるように、[`RealtimePlaybackTracker`][agents.realtime.model.RealtimePlaybackTracker] を使用してください。

[`examples/realtime/twilio/twilio_handler.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio/twilio_handler.py) の Twilio 例では、このパターンを示しています。

## ツール、承認、ハンドオフ、ガードレール

### 関数ツール

Realtime エージェントは、ライブ会話中に関数ツールをサポートします。

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

関数ツールは、実行前に人間の承認を必要とすることがあります。その場合、セッションは `tool_approval_required` を発行し、`approve_tool_call()` または `reject_tool_call()` を呼び出すまでツールの実行を一時停止します。

```python
async for event in session:
    if event.type == "tool_approval_required":
        await session.approve_tool_call(event.call_id)
```

具体的なサーバーサイド承認ループについては、[`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) を参照してください。human-in-the-loop のドキュメントでも、[Human in the loop](../human_in_the_loop.md) でこのフローを参照しています。

### ハンドオフ

Realtime ハンドオフにより、あるエージェントがライブ会話を別の専門エージェントに転送できます。

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

そのままの `RealtimeAgent` ハンドオフは自動でラップされ、`realtime_handoff(...)` を使うと名前、説明、検証、コールバック、可用性をカスタマイズできます。Realtime ハンドオフは通常のハンドオフ `input_filter` をサポートしていません。

### ガードレール

Realtime エージェントでは出力ガードレールのみがサポートされています。これらはすべての部分トークンごとではなく、デバウンスされた文字起こしの蓄積に対して実行され、例外を発生させる代わりに `guardrail_tripped` を発行します。

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

Realtime 出力ガードレールが発動すると、セッションはアクティブなレスポンスに割り込み、
`response.cancel` を強制し、`guardrail_tripped` を発行し、トリガーされた
ガードレール名を含むフォローアップのユーザーメッセージを送信して、モデルが代替レスポンスを生成できるようにします。音声プレイヤーは引き続き
`audio_interrupted` をリッスンし、ローカル再生をただちに停止する必要があります。これは、ガードレールがデバウンスされた文字起こしテキストに対して実行され、トリップワイヤーが作動した時点で一部の音声がすでにバッファリングされている可能性があるためです。

## SIP と電話

Python SDK には、[`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] によるファーストクラスの SIP アタッチフローが含まれています。

Realtime Calls API を通じて着信があり、結果として得られる `call_id` にエージェントセッションをアタッチしたい場合に使用します。

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

最初に通話を受け入れる必要があり、受け入れペイロードをエージェント由来のセッション設定と一致させたい場合は、`OpenAIRealtimeSIPModel.build_initial_session_payload(...)` を使用します。完全なフローは [`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) に示されています。

## 低レベルアクセスとカスタムエンドポイント

`session.model` を通じて基盤となるトランスポートオブジェクトにアクセスできます。

これは次の場合に使用します。

- `session.model.add_listener(...)` によるカスタムリスナー
- `response.create` や `session.update` などの raw クライアントイベント
- `model_config` を通じたカスタム `url`、`headers`、`api_key` の処理
- 既存の realtime 呼び出しへの `call_id` アタッチ

`RealtimeModelConfig` は次をサポートしています。

- `api_key`
- `url`
- `headers`
- `initial_model_settings`
- `playback_tracker`
- `call_id`

このリポジトリに同梱されている `call_id` の例は SIP です。より広範な Realtime API でも一部のサーバーサイド制御フローで `call_id` を使用しますが、それらはここでは Python 例としてパッケージ化されていません。

Azure OpenAI に接続する場合は、GA Realtime エンドポイント URL と明示的なヘッダーを渡してください。例:

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

`headers` を渡す場合、SDK は `Authorization` を自動的に追加しません。realtime エージェントでは、従来のベータパス (`/openai/realtime?api-version=...`) は避けてください。

## 関連情報

- [Realtime トランスポート](transport.md)
- [クイックスタート](quickstart.md)
- [OpenAI Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations/)
- [OpenAI Realtime server-side controls](https://developers.openai.com/api/docs/guides/realtime-server-controls/)
- [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)