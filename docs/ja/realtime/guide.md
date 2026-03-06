---
search:
  exclude: true
---
# Realtime エージェントガイド

このガイドでは、 OpenAI Agents SDK の realtime レイヤーが OpenAI Realtime API にどのように対応しているか、また Python SDK がその上にどのような追加動作を提供するかを説明します。

!!! warning "ベータ機能"

    Realtime エージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する可能性があります。

!!! note "開始ポイント"

    デフォルトの Python パスを使いたい場合は、まず [quickstart](quickstart.md) をお読みください。アプリでサーバーサイド WebSocket と SIP のどちらを使うべきか検討している場合は、 [Realtime transport](transport.md) をお読みください。ブラウザの WebRTC transport は Python SDK の対象外です。

## 概要

Realtime エージェントは Realtime API への長寿命接続を維持するため、モデルはテキストと音声を増分的に処理し、音声出力をストリーミングし、ツールを呼び出し、ターンごとに新しいリクエストを再開始することなく割り込みに対応できます。

主な SDK コンポーネントは次のとおりです。

-   **RealtimeAgent**: 1 つの realtime 専門エージェント向けの instructions、ツール、出力ガードレール、ハンドオフ
-   **RealtimeRunner**: 開始エージェントを realtime transport に接続するセッションファクトリー
-   **RealtimeSession**: 入力送信、イベント受信、履歴追跡、ツール実行を行うライブセッション
-   **RealtimeModel**: transport 抽象化。デフォルトは OpenAI のサーバーサイド WebSocket 実装です。

## セッションライフサイクル

一般的な realtime セッションは次のようになります。

1. 1 つ以上の `RealtimeAgent` を作成します。
2. 開始エージェントで `RealtimeRunner` を作成します。
3. `await runner.run()` を呼び出して `RealtimeSession` を取得します。
4. `async with session:` または `await session.enter()` でセッションに入ります。
5. `send_message()` または `send_audio()` でユーザー入力を送信します。
6. 会話が終了するまでセッションイベントを反復処理します。

テキスト専用実行とは異なり、 `runner.run()` は直ちに最終結果を生成しません。代わりに、ローカル履歴、バックグラウンドのツール実行、ガードレール状態、アクティブエージェント設定を transport レイヤーと同期し続けるライブセッションオブジェクトを返します。

デフォルトでは、 `RealtimeRunner` は `OpenAIRealtimeWebSocketModel` を使用するため、デフォルトの Python パスは Realtime API へのサーバーサイド WebSocket 接続です。別の `RealtimeModel` を渡した場合でも、同じセッションライフサイクルとエージェント機能が適用され、接続メカニズムのみ変更できます。

## エージェントとセッション設定

`RealtimeAgent` は通常の `Agent` 型より意図的に範囲が狭くなっています。

-   モデル選択はエージェント単位ではなくセッションレベルで設定します。
-   structured outputs はサポートされません。
-   音声は設定できますが、セッションですでに音声出力を生成した後は変更できません。
-   Instructions、関数ツール、ハンドオフ、フック、出力ガードレールは引き続き動作します。

`RealtimeSessionModelSettings` は、新しいネストされた `audio` 設定と古いフラットなエイリアスの両方をサポートします。新規コードではネスト形式を推奨します。

```python
runner = RealtimeRunner(
    starting_agent=agent,
    config={
        "model_settings": {
            "model_name": "gpt-realtime",
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

有用なセッションレベル設定は次のとおりです。

-   `audio.input.format`, `audio.output.format`
-   `audio.input.transcription`
-   `audio.input.noise_reduction`
-   `audio.input.turn_detection`
-   `audio.output.voice`, `audio.output.speed`
-   `output_modalities`
-   `tool_choice`
-   `prompt`
-   `tracing`

`RealtimeRunner(config=...)` の有用な実行レベル設定は次のとおりです。

-   `async_tool_calls`
-   `output_guardrails`
-   `guardrails_settings.debounce_text_length`
-   `tool_error_formatter`
-   `tracing_disabled`

型付き API 全体については、 [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] と [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings] を参照してください。

## 入出力

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

構造化メッセージは、 realtime 会話に画像入力を含める主な方法です。 [`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) の Web デモ例は、この方法で `input_image` メッセージを転送します。

### 音声入力

raw 音声バイトのストリーミングには [`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio] を使用します。

```python
await session.send_audio(audio_bytes)
```

サーバーサイドのターン検出が無効な場合、ターン境界のマークはユーザー側で行う必要があります。高レベルの簡易機能は次のとおりです。

```python
await session.send_audio(audio_bytes, commit=True)
```

より低レベルの制御が必要な場合は、基盤となる model transport を通じて `input_audio_buffer.commit` などの raw クライアントイベントも送信できます。

### 手動レスポンス制御

`session.send_message()` は高レベルパスを使ってユーザー入力を送信し、レスポンスを開始します。raw 音声バッファリングは、すべての設定で同じ動作を **自動的に** 行うわけではありません。

Realtime API レベルでは、手動ターン制御は raw `session.update` で `turn_detection` をクリアし、その後 `input_audio_buffer.commit` と `response.create` を自分で送信することを意味します。

ターンを手動管理している場合は、 model transport を通じて raw クライアントイベントを送信できます。

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

-   `turn_detection` が無効で、モデルがいつ応答するかを決めたい場合
-   レスポンスをトリガーする前にユーザー入力を検査または制御したい場合
-   帯域外レスポンスにカスタムプロンプトが必要な場合

[`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) の SIP 例では、開始時のあいさつを強制するために raw `response.create` を使用しています。

## イベント、履歴、割り込み

`RealtimeSession` は、必要時に raw model イベントを転送しつつ、より高レベルの SDK イベントを発行します。

価値の高いセッションイベントには次が含まれます。

-   `audio`, `audio_end`, `audio_interrupted`
-   `agent_start`, `agent_end`
-   `tool_start`, `tool_end`, `tool_approval_required`
-   `handoff`
-   `history_added`, `history_updated`
-   `guardrail_tripped`
-   `input_audio_timeout_triggered`
-   `error`
-   `raw_model_event`

UI 状態管理で最も有用なイベントは通常 `history_added` と `history_updated` です。これらは、ユーザーメッセージ、アシスタントメッセージ、ツール呼び出しを含むセッションのローカル履歴を `RealtimeItem` オブジェクトとして公開します。

### 割り込みと再生追跡

ユーザーがアシスタントを割り込むと、セッションは `audio_interrupted` を発行し、ユーザーが実際に聞いた内容とサーバーサイド会話が一致するよう履歴を更新します。

低遅延のローカル再生では、デフォルトの再生トラッカーで十分なことが多いです。リモート再生や遅延再生のシナリオ、特にテレフォニーでは、 [`RealtimePlaybackTracker`][agents.realtime.model.RealtimePlaybackTracker] を使用してください。これにより、割り込み時の切り詰めは、生成済み音声をすべて聞いた前提ではなく、実際の再生進捗に基づいて行われます。

[`examples/realtime/twilio/twilio_handler.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio/twilio_handler.py) の Twilio 例はこのパターンを示しています。

## ツール、承認、ハンドオフ、ガードレール

### 関数ツール

Realtime エージェントはライブ会話中の関数ツールをサポートします。

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

関数ツールは、実行前に人による承認を要求できます。その場合、セッションは `tool_approval_required` を発行し、 `approve_tool_call()` または `reject_tool_call()` を呼び出すまでツール実行を一時停止します。

```python
async for event in session:
    if event.type == "tool_approval_required":
        await session.approve_tool_call(event.call_id)
```

具体的なサーバーサイド承認ループは [`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) を参照してください。 human-in-the-loop ドキュメントでも [Human in the loop](../human_in_the_loop.md) でこのフローを参照しています。

### ハンドオフ

Realtime ハンドオフにより、 1 つのエージェントから別の専門エージェントへライブ会話を引き継げます。

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

素の `RealtimeAgent` ハンドオフは自動ラップされ、 `realtime_handoff(...)` では名前、説明、検証、コールバック、可用性をカスタマイズできます。Realtime ハンドオフは通常の handoff `input_filter` を **サポートしません** 。

### ガードレール

Realtime エージェントでサポートされるのは出力ガードレールのみです。これらは部分トークンごとではなく、デバウンスされた文字起こし蓄積に対して実行され、例外を送出する代わりに `guardrail_tripped` を発行します。

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

## SIP とテレフォニー

Python SDK には、 [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] によるファーストクラスの SIP アタッチフローが含まれます。

Realtime Calls API 経由で着信し、結果として得られる `call_id` にエージェントセッションをアタッチしたい場合に使用します。

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

`session.model` を通じて基盤 transport オブジェクトにアクセスできます。

これは次のような場合に使用します。

-   `session.model.add_listener(...)` によるカスタムリスナー
-   `response.create` や `session.update` などの raw クライアントイベント
-   `model_config` を通じたカスタム `url` 、 `headers` 、 `api_key` の処理
-   既存 realtime 通話への `call_id` アタッチ

`RealtimeModelConfig` は次をサポートします。

-   `api_key`
-   `url`
-   `headers`
-   `initial_model_settings`
-   `playback_tracker`
-   `call_id`

このリポジトリで提供される `call_id` の例は SIP です。より広い Realtime API でも一部のサーバーサイド制御フローで `call_id` を使用しますが、ここでは Python のコード例としては提供されていません。

Azure OpenAI に接続する場合は、 GA Realtime エンドポイント URL と明示的なヘッダーを渡してください。例:

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"api-key": "<your-azure-api-key>"},
    }
)
```

トークンベース認証では、 `headers` に bearer token を使用します。

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"authorization": f"Bearer {token}"},
    }
)
```

`headers` を渡した場合、 SDK は `Authorization` を自動追加しません。 realtime エージェントではレガシーなベータパス（ `/openai/realtime?api-version=...` ）を避けてください。

## 参考資料

-   [Realtime transport](transport.md)
-   [Quickstart](quickstart.md)
-   [OpenAI Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations/)
-   [OpenAI Realtime server-side controls](https://developers.openai.com/api/docs/guides/realtime-server-controls/)
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)