---
search:
  exclude: true
---
# Realtime エージェントガイド

このガイドでは、 OpenAI Agents SDK の realtime レイヤーが OpenAI Realtime API にどのように対応しているか、そして Python SDK がその上にどのような追加動作を加えるかを説明します。

!!! warning "Beta 機能"

    Realtime エージェントは beta 段階です。実装の改善に伴い、破壊的変更が入る可能性があります。

!!! note "開始ポイント"

    デフォルトの Python パスを使いたい場合は、まず [quickstart](quickstart.md) を読んでください。アプリでサーバーサイド WebSocket と SIP のどちらを使うべきか判断したい場合は、[Realtime transport](transport.md) を読んでください。ブラウザの WebRTC transport は Python SDK の対象外です。

## 概要

Realtime エージェントは Realtime API への長時間接続を維持するため、モデルはテキストと音声を段階的に処理し、音声出力をストリーミングし、ツールを呼び出し、毎ターン新しいリクエストを再開せずに割り込みを処理できます。

主な SDK コンポーネントは次のとおりです。

-   **RealtimeAgent**: 1 つの realtime 専門エージェント向けの instructions、ツール、出力ガードレール、ハンドオフ
-   **RealtimeRunner**: 開始エージェントを realtime transport に接続するセッションファクトリー
-   **RealtimeSession**: 入力送信、イベント受信、履歴追跡、ツール実行を行うライブセッション
-   **RealtimeModel**: transport 抽象化。デフォルトは OpenAI のサーバーサイド WebSocket 実装です。

## セッションライフサイクル

典型的な realtime セッションは次のようになります。

1. 1 つ以上の `RealtimeAgent` を作成します。
2. 開始エージェントで `RealtimeRunner` を作成します。
3. `await runner.run()` を呼び出して `RealtimeSession` を取得します。
4. `async with session:` または `await session.enter()` でセッションに入ります。
5. `send_message()` または `send_audio()` でユーザー入力を送信します。
6. 会話が終了するまでセッションイベントを反復処理します。

テキスト専用 run とは異なり、`runner.run()` は最終 result を即時には生成しません。transport レイヤーと同期を保ちながら、ローカル履歴、バックグラウンドツール実行、ガードレール状態、アクティブなエージェント設定を保持するライブセッションオブジェクトを返します。

デフォルトでは、`RealtimeRunner` は `OpenAIRealtimeWebSocketModel` を使用します。そのため、デフォルトの Python パスは Realtime API へのサーバーサイド WebSocket 接続です。別の `RealtimeModel` を渡した場合でも、同じセッションライフサイクルとエージェント機能が適用され、接続メカニズムのみ変更できます。

## エージェントとセッション設定

`RealtimeAgent` は通常の `Agent` 型より意図的に範囲が狭くなっています。

-   モデル選択はエージェントごとではなくセッションレベルで設定します。
-   structured outputs はサポートされていません。
-   Voice は設定できますが、セッションがすでに音声を生成した後は変更できません。
-   Instructions、関数ツール、ハンドオフ、フック、出力ガードレールはすべて引き続き利用できます。

`RealtimeSessionModelSettings` は、新しいネストされた `audio` 設定と古いフラットなエイリアスの両方をサポートします。新規コードではネスト形式を推奨し、新しい realtime エージェントには `gpt-realtime-1.5` から始めてください。

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

有用なセッションレベル設定には次が含まれます。

-   `audio.input.format`, `audio.output.format`
-   `audio.input.transcription`
-   `audio.input.noise_reduction`
-   `audio.input.turn_detection`
-   `audio.output.voice`, `audio.output.speed`
-   `output_modalities`
-   `tool_choice`
-   `prompt`
-   `tracing`

`RealtimeRunner(config=...)` での有用な run レベル設定には次が含まれます。

-   `async_tool_calls`
-   `output_guardrails`
-   `guardrails_settings.debounce_text_length`
-   `tool_error_formatter`
-   `tracing_disabled`

型付きの完全な仕様は [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] と [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings] を参照してください。

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

構造化メッセージは、realtime 会話に画像入力を含める主要な方法です。[`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) の Web デモ例では、この方法で `input_image` メッセージを転送しています。

### 音声入力

raw 音声バイトをストリーミングするには [`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio] を使用します。

```python
await session.send_audio(audio_bytes)
```

サーバーサイドの turn detection が無効な場合、ターン境界の指定はユーザー側の責任です。高レベルの簡易手段は次のとおりです。

```python
await session.send_audio(audio_bytes, commit=True)
```

より低レベルな制御が必要な場合は、基盤となる model transport を通じて `input_audio_buffer.commit` などの raw client event も送信できます。

### 手動レスポンス制御

`session.send_message()` は高レベルパスでユーザー入力を送信し、レスポンス開始も自動で行います。raw 音声バッファリングでは、すべての設定で同様に自動実行される **わけではありません** 。

Realtime API レベルでは、手動ターン制御は raw `session.update` で `turn_detection` をクリアし、その後 `input_audio_buffer.commit` と `response.create` を自分で送信することを意味します。

ターンを手動管理する場合は、model transport 経由で raw client event を送信できます。

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

-   `turn_detection` が無効で、モデルがいつ応答するかを自分で決めたい場合
-   レスポンスをトリガーする前にユーザー入力を検査またはゲートしたい場合
-   out-of-band レスポンス向けにカスタムプロンプトが必要な場合

[`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) の SIP 例では、raw `response.create` を使って開始時の挨拶を強制しています。

## イベント、履歴、割り込み

`RealtimeSession` は高レベル SDK イベントを発行しつつ、必要時には raw model event も転送します。

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

UI 状態管理で特に有用なのは通常 `history_added` と `history_updated` です。これらは、ユーザーメッセージ、assistant メッセージ、ツール呼び出しを含むセッションのローカル履歴を `RealtimeItem` オブジェクトとして公開します。

### 割り込みと再生追跡

ユーザーが assistant を割り込んだ場合、セッションは `audio_interrupted` を発行し、サーバーサイド会話がユーザーの実際の聴取内容と一致するよう履歴を更新します。

低遅延のローカル再生では、デフォルトの再生トラッカーで十分なことが多いです。リモート再生や遅延再生のシナリオ、特に電話では、すべての生成音声がすでに聴取済みと仮定するのではなく、実際の再生進捗に基づいて割り込み切り詰めを行うために [`RealtimePlaybackTracker`][agents.realtime.model.RealtimePlaybackTracker] を使用してください。

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

関数ツールは、実行前に人間の承認を必要とするようにできます。その場合、セッションは `tool_approval_required` を発行し、`approve_tool_call()` または `reject_tool_call()` を呼び出すまでツール実行を一時停止します。

```python
async for event in session:
    if event.type == "tool_approval_required":
        await session.approve_tool_call(event.call_id)
```

具体的なサーバーサイド承認ループは [`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) を参照してください。human-in-the-loop ドキュメントでも [Human in the loop](../human_in_the_loop.md) でこのフローを参照しています。

### ハンドオフ

Realtime ハンドオフでは、あるエージェントがライブ会話を別の専門エージェントへ転送できます。

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

素の `RealtimeAgent` ハンドオフは自動ラップされ、`realtime_handoff(...)` では名前、説明、検証、コールバック、可用性をカスタマイズできます。Realtime ハンドオフは通常の handoff `input_filter` をサポートしません。

### ガードレール

Realtime エージェントでサポートされるのは出力ガードレールのみです。これらは各部分 token ごとではなく、デバウンスされた transcript 蓄積に対して実行され、例外を送出する代わりに `guardrail_tripped` を発行します。

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

Python SDK には [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] による第一級の SIP 接続フローが含まれています。

Realtime Calls API 経由で着信し、結果として得られる `call_id` にエージェントセッションを接続したい場合に使用します。

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

まず通話を受け付ける必要があり、受け付けペイロードをエージェント由来のセッション設定に一致させたい場合は、`OpenAIRealtimeSIPModel.build_initial_session_payload(...)` を使用してください。完全なフローは [`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) にあります。

## 低レベルアクセスとカスタムエンドポイント

`session.model` から基盤 transport オブジェクトにアクセスできます。

必要な場合に使用します。

-   `session.model.add_listener(...)` によるカスタムリスナー
-   `response.create` や `session.update` などの raw client event
-   `model_config` 経由のカスタム `url`、`headers`、`api_key` 処理
-   既存 realtime 通話への `call_id` 接続

`RealtimeModelConfig` は次をサポートします。

-   `api_key`
-   `url`
-   `headers`
-   `initial_model_settings`
-   `playback_tracker`
-   `call_id`

このリポジトリに含まれる `call_id` の例は SIP です。より広い Realtime API では一部のサーバーサイド制御フローにも `call_id` を使いますが、ここでは Python 例としては提供されていません。

Azure OpenAI に接続する場合は、 GA Realtime endpoint URL と明示的な headers を渡してください。例:

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"api-key": "<your-azure-api-key>"},
    }
)
```

トークンベース認証では、`headers` に bearer token を使用します。

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"authorization": f"Bearer {token}"},
    }
)
```

`headers` を渡した場合、SDK は `Authorization` を自動追加しません。realtime エージェントではレガシー beta パス（`/openai/realtime?api-version=...`）を避けてください。

## 参考資料

-   [Realtime transport](transport.md)
-   [Quickstart](quickstart.md)
-   [OpenAI Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations/)
-   [OpenAI Realtime server-side controls](https://developers.openai.com/api/docs/guides/realtime-server-controls/)
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)