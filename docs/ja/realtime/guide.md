---
search:
  exclude: true
---
# Realtime エージェントガイド

このガイドでは、 OpenAI Agents SDK の realtime レイヤーが OpenAI Realtime API にどのように対応するか、および Python SDK がその上に追加する挙動について説明します。

!!! note "はじめに"

    デフォルトの Python パスを使いたい場合は、まず [クイックスタート](quickstart.md) を読んでください。アプリでサーバー側 WebSocket または SIP のどちらを使うべきかを判断している場合は、 [Realtime トランスポート](transport.md) を読んでください。ブラウザー WebRTC トランスポートは Python SDK には含まれていません。

## 概要

Realtime エージェントは Realtime API への長寿命の接続を開いたままにするため、モデルはテキストと音声を段階的に処理し、音声出力をストリーミングし、ツールを呼び出し、中断を処理できます。ターンごとに新しいリクエストを再開始する必要はありません。

主な SDK コンポーネントは次のとおりです。

-   **RealtimeAgent**: 1 つの realtime 専門エージェント向けの instructions、tools、出力ガードレール、ハンドオフ
-   **RealtimeRunner**: 開始エージェントを realtime トランスポートに接続するセッションファクトリ
-   **RealtimeSession**: 入力を送信し、イベントを受信し、履歴を追跡し、ツールを実行するライブセッション
-   **RealtimeModel**: トランスポート抽象化。デフォルトは OpenAI のサーバー側 WebSocket 実装です。

## セッションライフサイクル

一般的な realtime セッションは次のようになります。

1. 1 つ以上の `RealtimeAgent` を作成します。
2. 開始エージェントを指定して `RealtimeRunner` を作成します。
3. `await runner.run()` を呼び出して `RealtimeSession` を取得します。
4. `async with session:` または `await session.enter()` でセッションに入ります。
5. `send_message()` または `send_audio()` でユーザー入力を送信します。
6. 会話が終了するまでセッションイベントを反復処理します。

テキストのみの実行とは異なり、 `runner.run()` は最終的な実行結果をすぐには生成しません。ローカル履歴、バックグラウンドでのツール実行、ガードレール状態、アクティブなエージェント設定をトランスポート層と同期し続けるライブセッションオブジェクトを返します。

デフォルトでは、 `RealtimeRunner` は `OpenAIRealtimeWebSocketModel` を使用するため、デフォルトの Python パスは Realtime API へのサーバー側 WebSocket 接続です。別の `RealtimeModel` を渡した場合でも、接続の仕組みは変わる可能性がありますが、同じセッションライフサイクルとエージェント機能が引き続き適用されます。

## エージェントとセッション設定

`RealtimeAgent` は通常の `Agent` 型より意図的に機能範囲が絞られています。

-   モデル選択はエージェントごとではなく、セッションレベルで設定されます。
-   Structured outputs はサポートされていません。
-   voice は設定できますが、セッションがすでに発話音声を生成した後は変更できません。
-   instructions、関数ツール、ハンドオフ、フック、出力ガードレールはすべて引き続き機能します。

`RealtimeSessionModelSettings` は、新しいネストされた `audio` 設定と、古いフラットなエイリアスの両方をサポートします。新しいコードではネストされた形式を推奨し、新しい realtime エージェントでは `gpt-realtime-2.1` から始めてください。

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

`RealtimeRunner(config=...)` の有用な run レベル設定には次のものがあります。

-   `async_tool_calls`
-   `output_guardrails`
-   `guardrails_settings.debounce_text_length`
-   `tool_error_formatter`
-   `tracing_disabled`

完全な型付きインターフェースについては、 [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] と [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings] を参照してください。

## 入力と出力

### テキストと構造化ユーザーメッセージ

プレーンテキストまたは構造化された realtime メッセージには、 [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用します。

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

構造化メッセージは、 realtime 会話に画像入力を含める主な方法です。 [`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) のサンプル Web デモでは、この方法で `input_image` メッセージを転送しています。

### 音声入力

生の音声バイトをストリーミングするには、 [`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio] を使用します。

```python
await session.send_audio(audio_bytes)
```

サーバー側のターン検出が無効な場合、ターン境界をマークする責任は開発者側にあります。高レベルの便利な方法は次のとおりです。

```python
await session.send_audio(audio_bytes, commit=True)
```

より低レベルの制御が必要な場合は、基盤となるモデルトランスポートを通じて `input_audio_buffer.commit` などの raw クライアントイベントを送信することもできます。

### 手動応答制御

`session.send_message()` は高レベルパスを使ってユーザー入力を送信し、応答を開始します。生の音声バッファリングは、すべての設定で同じことを自動的に行うわけでは **ありません** 。

Realtime API レベルでは、手動のターン制御とは、 raw `session.update` で `turn_detection` をクリアし、その後に `input_audio_buffer.commit` と `response.create` を自分で送信することを意味します。

ターンを手動で管理している場合は、モデルのトランスポートを通じて raw クライアントイベントを送信できます。

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

-   `turn_detection` が無効で、モデルがいつ応答すべきかを自分で決めたい場合
-   応答をトリガーする前にユーザー入力を検査またはゲートしたい場合
-   アウトオブバンド応答にカスタムプロンプトが必要な場合

[`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) の SIP の例では、開始時の挨拶を強制するために raw `response.create` を使用しています。

## イベント、履歴、中断

`RealtimeSession` は、必要なときには raw モデルイベントも転送しつつ、より高レベルの SDK イベントを発行します。

有用性の高いセッションイベントには次のものがあります。

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

### 中断と再生トラッキング

ユーザーがアシスタントを中断すると、セッションは `audio_interrupted` を発行し、履歴を更新して、サーバー側の会話がユーザーが実際に聞いた内容と一致し続けるようにします。

低レイテンシーのローカル再生では、デフォルトの再生トラッカーで十分なことが多いです。リモート再生や遅延再生のシナリオ、特にテレフォニーでは、 [`RealtimePlaybackTracker`][agents.realtime.model.RealtimePlaybackTracker] を使用してください。これにより、生成された音声がすべてすでに聞かれたと仮定するのではなく、実際の再生進行に基づいて中断時の切り詰めが行われます。

[`examples/realtime/twilio/twilio_handler.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio/twilio_handler.py) の Twilio の例はこのパターンを示しています。

## ツール、承認、ハンドオフ、ガードレール

### 関数ツール

Realtime エージェントはライブ会話中に関数ツールをサポートします。

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

関数ツールは実行前に人間の承認を要求できます。その場合、セッションは `tool_approval_required` を発行し、 `approve_tool_call()` または `reject_tool_call()` を呼び出すまでツール実行を一時停止します。

ツールに入力ガードレールもある場合、それらのガードレールは承認後、実行の直前に実行されます。承認イベントが発行される前にそれらを実行するには、 `RealtimeRunner(..., config={"tool_execution": {"pre_approval_tool_input_guardrails": True}})` で runner を作成します。この事前承認チェックに合格した呼び出しも、承認後、実行前にもう一度チェックされます。

```python
async for event in session:
    if event.type == "tool_approval_required":
        await session.approve_tool_call(event.call_id)
```

具体的なサーバー側承認ループについては、 [`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) を参照してください。人間参加型のドキュメントも、 [人間を介した処理](../human_in_the_loop.md) でこのフローを参照しています。

### ハンドオフ

Realtime ハンドオフにより、 1 つのエージェントがライブ会話を別の専門エージェントへ転送できます。

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

そのままの `RealtimeAgent` ハンドオフは自動的にラップされ、 `realtime_handoff(...)` では名前、説明、検証、コールバック、利用可否をカスタマイズできます。Realtime ハンドオフは通常のハンドオフの `input_filter` を **サポートしていません** 。

### ガードレール

Realtime エージェントは、エージェント応答に対する出力ガードレールと、関数ツール呼び出しに対する入力ガードレールをサポートします。出力ガードレールは、部分的なトークンごとではなく、デバウンスされたトランスクリプトの蓄積に対して実行され、例外を発生させる代わりに `guardrail_tripped` を発行します。

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

realtime 出力ガードレールが作動すると、セッションはアクティブな応答を中断し、 `response.cancel` を強制し、 `guardrail_tripped` を発行し、作動したガードレールの名前を示す後続のユーザーメッセージを送信して、モデルが代替応答を生成できるようにします。音声プレーヤーはそれでも `audio_interrupted` をリッスンし、ローカル再生をただちに停止する必要があります。これは、ガードレールがデバウンスされたトランスクリプトテキストに対して実行され、トリップワイヤーが作動した時点で一部の音声がすでにバッファリングされている可能性があるためです。

## SIP とテレフォニー

Python SDK には、 [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] を介したファーストクラスの SIP アタッチフローが含まれています。

Realtime Calls API 経由で通話が到着し、得られた `call_id` にエージェントセッションをアタッチしたい場合に使用します。

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

先に通話を受け付ける必要があり、 accept ペイロードをエージェント由来のセッション設定と一致させたい場合は、 `OpenAIRealtimeSIPModel.build_initial_session_payload(...)` を使用します。完全なフローは [`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) に示されています。

## 低レベルアクセスとカスタムエンドポイント

`session.model` を通じて基盤となるトランスポートオブジェクトにアクセスできます。

次が必要な場合に使用します。

-   `session.model.add_listener(...)` によるカスタムリスナー
-   `response.create` や `session.update` などの raw クライアントイベント
-   `model_config` によるカスタムの `url`、 `headers`、 `api_key` 処理
-   既存の realtime 通話への `call_id` アタッチ

`RealtimeModelConfig` は次をサポートします。

-   `api_key`
-   `url`
-   `headers`
-   `initial_model_settings`
-   `playback_tracker`
-   `call_id`

このリポジトリに同梱されている `call_id` のコード例は SIP です。より広範な Realtime API でも、一部のサーバー側制御フローに `call_id` を使用しますが、それらはここでは Python のコード例としてパッケージ化されていません。

Azure OpenAI に接続する場合は、 GA Realtime エンドポイント URL と明示的なヘッダーを渡します。例:

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"api-key": "<your-azure-api-key>"},
    }
)
```

トークンベースの認証では、 `headers` に Bearer トークンを使用します。

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"authorization": f"Bearer {token}"},
    }
)
```

`headers` を渡すと、 SDK は `Authorization` を自動的には追加しません。realtime エージェントでは、従来の beta パス (`/openai/realtime?api-version=...`) は避けてください。

## 関連資料

-   [Realtime トランスポート](transport.md)
-   [クイックスタート](quickstart.md)
-   [OpenAI Realtime 会話](https://developers.openai.com/api/docs/guides/realtime-conversations/)
-   [OpenAI Realtime サーバー側コントロール](https://developers.openai.com/api/docs/guides/realtime-server-controls/)
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)