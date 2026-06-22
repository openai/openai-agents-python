---
search:
  exclude: true
---
# Realtime エージェントガイド

本ガイドでは、OpenAI Agents SDK のリアルタイムレイヤーが OpenAI Realtime API にどのように対応し、その上で Python SDK がどのような追加の動作を加えるかを説明します。

!!! note "はじめに"

    デフォルトの Python パスを使いたい場合は、まず [クイックスタート](quickstart.md) を読んでください。アプリでサーバー側 WebSocket と SIP のどちらを使うべきか判断している場合は、[Realtime トランスポート](transport.md) を読んでください。ブラウザーの WebRTC トランスポートは Python SDK の一部ではありません。

## 概要

Realtime エージェントは Realtime API への長期間維持される接続を開いたままにするため、モデルはテキストと音声をインクリメンタルに処理し、音声出力をストリーミングし、ツールを呼び出し、各ターンで新しいリクエストを再開することなく割り込みを処理できます。

主な SDK コンポーネントは次のとおりです。

-   **RealtimeAgent**: 1 つのリアルタイム専門エージェント向けの instructions、tools、出力ガードレール、ハンドオフ
-   **RealtimeRunner**: 開始エージェントを realtime トランスポートに接続するセッションファクトリー
-   **RealtimeSession**: 入力を送信し、イベントを受信し、履歴を追跡し、ツールを実行するライブセッション
-   **RealtimeModel**: トランスポート抽象化です。デフォルトは OpenAI のサーバー側 WebSocket 実装です。

## セッションライフサイクル

一般的な realtime セッションは次のようになります。

1. 1 つ以上の `RealtimeAgent` を作成します。
2. 開始エージェントを指定して `RealtimeRunner` を作成します。
3. `await runner.run()` を呼び出して `RealtimeSession` を取得します。
4. `async with session:` または `await session.enter()` でセッションに入ります。
5. `send_message()` または `send_audio()` でユーザー入力を送信します。
6. 会話が終了するまでセッションイベントを反復処理します。

テキストのみの実行とは異なり、`runner.run()` は最終的な実行結果をすぐには生成しません。トランスポート層と同期した状態で、ローカル履歴、バックグラウンドのツール実行、ガードレール状態、有効なエージェント設定を保持するライブセッションオブジェクトを返します。

デフォルトでは、`RealtimeRunner` は `OpenAIRealtimeWebSocketModel` を使用するため、デフォルトの Python パスは Realtime API へのサーバー側 WebSocket 接続です。別の `RealtimeModel` を渡した場合も、同じセッションライフサイクルとエージェント機能は適用されますが、接続の仕組みは変わる可能性があります。

## エージェントとセッションの設定

`RealtimeAgent` は通常の `Agent` 型より意図的に範囲が狭くなっています。

-   モデルの選択はエージェントごとではなく、セッションレベルで設定します。
-   Structured outputs はサポートされていません。
-   voice は設定できますが、セッションがすでに発話音声を生成した後は変更できません。
-   instructions、関数ツール、ハンドオフ、フック、出力ガードレールはすべて引き続き機能します。

`RealtimeSessionModelSettings` は、新しいネストされた `audio` 設定と、従来のフラットなエイリアスの両方をサポートしています。新しいコードではネスト形式を優先し、新しい realtime エージェントでは `gpt-realtime-2` から始めてください。

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

有用なセッションレベル設定には次のものがあります。

-   `audio.input.format`, `audio.output.format`
-   `audio.input.transcription`
-   `audio.input.noise_reduction`
-   `audio.input.turn_detection`
-   `audio.output.voice`, `audio.output.speed`
-   `output_modalities`
-   `tool_choice`
-   `prompt`
-   `tracing`

`RealtimeRunner(config=...)` の有用な実行レベル設定には次のものがあります。

-   `async_tool_calls`
-   `output_guardrails`
-   `guardrails_settings.debounce_text_length`
-   `tool_error_formatter`
-   `tracing_disabled`

完全な型付きインターフェイスについては、[`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] と [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings] を参照してください。

## 入力と出力

### テキストと構造化ユーザーメッセージ

プレーンテキストまたは構造化された realtime メッセージには、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用します。

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

構造化メッセージは、realtime 会話に画像入力を含める主な方法です。[`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) のサンプル Web デモでは、この方法で `input_image` メッセージを転送しています。

### 音声入力

生の音声バイトをストリーミングするには、[`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio] を使用します。

```python
await session.send_audio(audio_bytes)
```

サーバー側ターン検出が無効な場合、ターン境界を示す責任はあなたにあります。高レベルの便利な方法は次のとおりです。

```python
await session.send_audio(audio_bytes, commit=True)
```

より低レベルの制御が必要な場合は、基盤となるモデルトランスポートを通じて `input_audio_buffer.commit` などの raw クライアントイベントを送信することもできます。

### 手動レスポンス制御

`session.send_message()` は高レベルのパスを使ってユーザー入力を送信し、レスポンスを開始します。生の音声バッファリングは、すべての設定で同じことを自動的に行うわけでは **ありません**。

Realtime API レベルでは、手動ターン制御とは、raw の `session.update` で `turn_detection` をクリアし、その後 `input_audio_buffer.commit` と `response.create` を自分で送信することを意味します。

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

このパターンは次の場合に有用です。

-   `turn_detection` が無効で、モデルがいつ応答すべきかを自分で判断したい場合
-   レスポンスをトリガーする前にユーザー入力を検査またはゲートしたい場合
-   帯域外レスポンス用のカスタムプロンプトが必要な場合

[`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) の SIP 例では、冒頭のあいさつを強制するために raw の `response.create` を使用しています。

## イベント、履歴、割り込み

`RealtimeSession` は、必要に応じて raw モデルイベントも転送しながら、より高レベルの SDK イベントを発行します。

特に重要なセッションイベントには次のものがあります。

-   `audio`, `audio_end`, `audio_interrupted`
-   `agent_start`, `agent_end`
-   `tool_start`, `tool_end`, `tool_approval_required`
-   `handoff`
-   `history_added`, `history_updated`
-   `guardrail_tripped`
-   `input_audio_timeout_triggered`
-   `error`
-   `raw_model_event`

UI 状態に最も有用なイベントは通常 `history_added` と `history_updated` です。これらは、ユーザーメッセージ、アシスタントメッセージ、ツール呼び出しを含む、セッションのローカル履歴を `RealtimeItem` オブジェクトとして公開します。

### 割り込みと再生トラッキング

ユーザーがアシスタントに割り込むと、セッションは `audio_interrupted` を発行し、サーバー側の会話がユーザーが実際に聞いた内容と一致し続けるように履歴を更新します。

低遅延のローカル再生では、デフォルトの再生トラッカーで十分な場合が多いです。リモート再生や遅延のある再生シナリオ、特にテレフォニーでは、生成された音声がすべてすでに聞かれたと仮定するのではなく、実際の再生進行状況に基づいて割り込み時の切り詰めを行うために、[`RealtimePlaybackTracker`][agents.realtime.model.RealtimePlaybackTracker] を使用してください。

[`examples/realtime/twilio/twilio_handler.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio/twilio_handler.py) の Twilio 例はこのパターンを示しています。

## ツール、承認、ハンドオフ、ガードレール

### 関数ツール

Realtime エージェントはライブ会話中の関数ツールをサポートしています。

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

関数ツールは、実行前に人間による承認を必要とする場合があります。その場合、セッションは `tool_approval_required` を発行し、`approve_tool_call()` または `reject_tool_call()` を呼び出すまでツール実行を一時停止します。

そのツールに入力ガードレールもある場合、それらのガードレールは承認後、実行直前に実行されます。承認イベントが発行される前にそれらを実行するには、`RealtimeRunner(..., config={"tool_execution": {"pre_approval_tool_input_guardrails": True}})` で runner を作成します。この事前承認チェックを通過した呼び出しも、承認後、実行前に再度チェックされます。

```python
async for event in session:
    if event.type == "tool_approval_required":
        await session.approve_tool_call(event.call_id)
```

具体的なサーバー側承認ループについては、[`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) を参照してください。人間の関与に関するドキュメントでも、[人間の関与](../human_in_the_loop.md) でこのフローを参照しています。

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
    handoffs=[
        realtime_handoff(
            billing_agent,
            tool_description_override="Transfer to billing support",
        )
    ],
)
```

素の `RealtimeAgent` ハンドオフは自動的にラップされ、`realtime_handoff(...)` を使うと名前、説明、検証、コールバック、利用可否をカスタマイズできます。Realtime ハンドオフは、通常のハンドオフの `input_filter` をサポートしていません。

### ガードレール

Realtime エージェントは、エージェントのレスポンスに対する出力ガードレールと、関数ツール呼び出しに対する入力ガードレールをサポートしています。出力ガードレールは、部分トークンごとではなくデバウンスされたトランスクリプトの蓄積に対して実行され、例外を発生させる代わりに `guardrail_tripped` を発行します。

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

realtime 出力ガードレールがトリップすると、セッションはアクティブなレスポンスに割り込み、強制的に
`response.cancel` を実行し、`guardrail_tripped` を発行し、モデルが代替レスポンスを生成できるように、トリップした
ガードレールの名前を示すフォローアップのユーザーメッセージを送信します。ガードレールは
デバウンスされたトランスクリプトテキストに対して実行され、トリップワイヤーが発火した時点ですでに音声がバッファリングされている場合があるため、音声プレイヤーは引き続き
`audio_interrupted` をリッスンし、ローカル再生をただちに停止する必要があります。

## SIP とテレフォニー

Python SDK には、[`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] によるファーストクラスの SIP アタッチフローが含まれています。

Realtime Calls API 経由で通話が到着し、その結果得られる `call_id` にエージェントセッションをアタッチしたい場合に使用します。

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

先に通話を受け入れる必要があり、accept ペイロードをエージェント由来のセッション設定と一致させたい場合は、`OpenAIRealtimeSIPModel.build_initial_session_payload(...)` を使用します。完全なフローは [`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) に示されています。

## 低レベルアクセスとカスタムエンドポイント

`session.model` を通じて、基盤となるトランスポートオブジェクトにアクセスできます。

次の場合に使用します。

-   `session.model.add_listener(...)` によるカスタムリスナー
-   `response.create` や `session.update` などの raw クライアントイベント
-   `model_config` を通じたカスタム `url`、`headers`、または `api_key` の処理
-   既存の realtime 通話への `call_id` アタッチ

`RealtimeModelConfig` は次をサポートしています。

-   `api_key`
-   `url`
-   `headers`
-   `initial_model_settings`
-   `playback_tracker`
-   `call_id`

このリポジトリに同梱されている `call_id` のコード例は SIP です。より広範な Realtime API でも、一部のサーバー側制御フローで `call_id` が使用されますが、ここではそれらは Python のコード例として同梱されていません。

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

`headers` を渡した場合、SDK は `Authorization` を自動的には追加しません。Realtime エージェントでは、レガシーの beta パス (`/openai/realtime?api-version=...`) を避けてください。

## 参考資料

-   [Realtime トランスポート](transport.md)
-   [クイックスタート](quickstart.md)
-   [OpenAI Realtime 会話](https://developers.openai.com/api/docs/guides/realtime-conversations/)
-   [OpenAI Realtime サーバー側制御](https://developers.openai.com/api/docs/guides/realtime-server-controls/)
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)