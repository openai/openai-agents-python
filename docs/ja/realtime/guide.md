---
search:
  exclude: true
---
# Realtime エージェントガイド

本ガイドでは、OpenAI Agents SDK の Realtime レイヤーが OpenAI Realtime API にどのように対応しているか、および Python SDK がその上に追加する動作について説明します。

!!! note "まずはこちら"

    Python の標準的な利用方法については、最初に[クイックスタート](quickstart.md)をお読みください。アプリでサーバー側 WebSocket と SIP のどちらを使用するか検討している場合は、[Realtime トランスポート](transport.md)をお読みください。ブラウザーの WebRTC トランスポートは Python SDK に含まれていません。

## 概要

Realtime エージェントは Realtime API への長時間接続を維持するため、モデルはターンごとに新しいリクエストを最初から開始することなく、テキストと音声を逐次処理し、音声出力をストリーミングし、ツールを呼び出し、中断を処理できます。

SDK の主なコンポーネントは次のとおりです。

-   **RealtimeAgent**: 1 つの Realtime 専門エージェント向けの指示、ツール、出力ガードレール、ハンドオフ
-   **RealtimeRunner**: 開始エージェントを Realtime トランスポートに接続するセッションファクトリー
-   **RealtimeSession**: 入力を送信し、イベントを受信し、履歴を追跡し、ツールを実行するライブセッション
-   **RealtimeModel**: トランスポートの抽象化。デフォルトは OpenAI のサーバー側 WebSocket 実装です。

## セッションのライフサイクル

一般的な Realtime セッションは次のようになります。

1. 1 つ以上の `RealtimeAgent` を作成します。
2. 開始エージェントを指定して `RealtimeRunner` を作成します。
3. `await runner.run()` を呼び出して `RealtimeSession` を取得します。
4. `async with session:` または `await session.enter()` を使用してセッションに入ります。
5. `send_message()` または `send_audio()` を使用してユーザー入力を送信します。
6. 会話が終了するまでセッションイベントを反復処理します。

テキストのみの実行とは異なり、`runner.run()` は最終的な実行結果をすぐには生成しません。代わりに、ローカル履歴、バックグラウンドでのツール実行、ガードレールの状態、アクティブなエージェント設定をトランスポートレイヤーと同期し続けるライブセッションオブジェクトを返します。

デフォルトでは、`RealtimeRunner` は `OpenAIRealtimeWebSocketModel` を使用するため、Python の標準的な利用方法では Realtime API へのサーバー側 WebSocket 接続が使用されます。別の `RealtimeModel` を渡した場合も、接続の仕組みは変えられますが、同じセッションライフサイクルとエージェント機能が引き続き適用されます。

## エージェントとセッションの設定

`RealtimeAgent` は意図的に通常の `Agent` 型よりも対象範囲が限定されています。

-   モデルの選択はエージェントごとではなく、セッションレベルで設定します。
-   structured outputs はサポートされていません。
-   音声は設定できますが、セッションが発話音声を生成した後は変更できません。
-   指示、関数ツール、ハンドオフ、フック、出力ガードレールはすべて引き続き機能します。

`RealtimeSessionModelSettings` は、新しいネスト形式の `audio` 設定と従来のフラット形式のエイリアスの両方をサポートしています。新しいコードではネスト形式を推奨します。また、新しい Realtime エージェントでは `gpt-realtime-2.1` から始めてください。

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

主なセッションレベルの設定は次のとおりです。

-   `audio.input.format`, `audio.output.format`
-   `audio.input.transcription`
-   `audio.input.noise_reduction`
-   `audio.input.turn_detection`
-   `audio.output.voice`, `audio.output.speed`
-   `output_modalities`
-   `tool_choice`
-   `prompt`
-   `tracing`

`RealtimeRunner(config=...)` の主な実行レベルの設定は次のとおりです。

-   `async_tool_calls`
-   `output_guardrails`
-   `guardrails_settings.debounce_text_length`
-   `tool_error_formatter`
-   `tracing_disabled`

型付けされたインターフェース全体については、[`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] および [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings] を参照してください。

## 入出力

### テキストと構造化ユーザーメッセージ

プレーンテキストまたは構造化された Realtime メッセージには、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用します。

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

構造化メッセージは、Realtime 会話に画像入力を含めるための主な方法です。[`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) の Web デモのコード例では、この方法で `input_image` メッセージを転送しています。

### 音声入力

raw 音声バイトをストリーミングするには、[`session.send_audio()`][agents.realtime.session.RealtimeSession.send_audio] を使用します。

```python
await session.send_audio(audio_bytes)
```

サーバー側のターン検出が無効になっている場合、ターンの境界を示す処理はご自身で行う必要があります。高レベルの便利な方法は次のとおりです。

```python
await session.send_audio(audio_bytes, commit=True)
```

より低レベルの制御が必要な場合は、基盤となるモデルトランスポートを介して、`input_audio_buffer.commit` などの raw クライアントイベントを送信することもできます。

### 手動レスポンス制御

`session.send_message()` は、高レベルの経路を使用してユーザー入力を送信し、レスポンスを開始します。raw 音声のバッファリングでは、すべての設定で同じ処理が **自動的に行われるわけではありません**。

Realtime API レベルでターンを手動制御するには、raw の `session.update` で `turn_detection` をクリアしてから、ご自身で `input_audio_buffer.commit` と `response.create` を送信します。

ターンを手動で管理する場合は、モデルトランスポートを介して raw クライアントイベントを送信できます。

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

このパターンは、次の場合に役立ちます。

-   `turn_detection` が無効であり、モデルが応答するタイミングを指定したい場合
-   レスポンスを開始する前にユーザー入力を確認または制御したい場合
-   帯域外レスポンス用のカスタムプロンプトが必要な場合

[`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) の SIP のコード例では、raw の `response.create` を使用して冒頭の挨拶を強制的に生成しています。

## イベント、履歴、中断

`RealtimeSession` は高レベルの SDK イベントを発行しながら、必要に応じて raw モデルイベントも転送します。

重要なセッションイベントは次のとおりです。

-   `audio`, `audio_end`, `audio_interrupted`
-   `agent_start`, `agent_end`
-   `tool_start`, `tool_end`, `tool_approval_required`
-   `handoff`
-   `history_added`, `history_updated`
-   `guardrail_tripped`
-   `input_audio_timeout_triggered`
-   `error`
-   `raw_model_event`

UI の状態に最も役立つイベントは、通常 `history_added` と `history_updated` です。これらは、ユーザーメッセージ、アシスタントメッセージ、ツール呼び出しなど、セッションのローカル履歴を `RealtimeItem` オブジェクトとして公開します。

### 使用量の集計

完了したモデルレスポンスに使用量が含まれている場合、OpenAI の Realtime モデルは `raw_model_event` 内で [`RealtimeModelUsageEvent`][agents.realtime.model_events.RealtimeModelUsageEvent] を発行します。その `usage` フィールドにはレスポンスのトークン数が含まれ、`input_tokens_details` と `output_tokens_details` では任意のモダリティ別内訳が提供されます。

セッションは各レスポンスの使用量を、共有される [`RunContextWrapper.usage`][agents.run_context.RunContextWrapper.usage] にも追加します。ライブセッションの累積使用量を確認するには、`agent_end` などの後続の高レベルイベントで `event.info.context.usage` から読み取ります。

```python
from agents.realtime import RealtimeModelUsageEvent

async for event in session:
    if event.type == "raw_model_event" and isinstance(
        event.data, RealtimeModelUsageEvent
    ):
        response_usage = event.data.usage
        print("Response tokens:", response_usage.total_tokens)
        print("Input modalities:", event.data.input_tokens_details)
        print("Output modalities:", event.data.output_tokens_details)
    elif event.type == "agent_end":
        session_usage = event.info.context.usage
        print("Session tokens:", session_usage.total_tokens)
```

使用量は、モデルプロバイダーが完了したレスポンスにその情報を含めた場合にのみ報告されます。累積値の対象は、その `RealtimeSession` が受信したレスポンスです。複数のセッションをまたぐ合計値ではありません。

### 中断と再生トラッキング

ユーザーがアシスタントを中断すると、セッションは `audio_interrupted` を発行し、サーバー側の会話がユーザーに実際に聞こえた内容と一致するように履歴を更新します。

低遅延のローカル再生では、通常、デフォルトの再生トラッカーで十分です。リモート再生や遅延再生、特にテレフォニーでは、生成された音声がすべて再生済みであると想定するのではなく、実際の再生進捗に基づいて中断時の切り詰めを行うために、[`RealtimePlaybackTracker`][agents.realtime.model.RealtimePlaybackTracker] を使用してください。

[`examples/realtime/twilio/twilio_handler.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio/twilio_handler.py) の Twilio のコード例は、このパターンを示しています。

## ツール、承認、ハンドオフ、ガードレール

### 関数ツール

Realtime エージェントは、ライブ会話中の関数ツールをサポートしています。

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

### ツールの承認

関数ツールでは、実行前に人間による承認を必須にできます。その場合、セッションは `tool_approval_required` を発行し、`approve_tool_call()` または `reject_tool_call()` が呼び出されるまでツールの実行を一時停止します。

ツールに入力ガードレールも設定されている場合、そのガードレールは承認後、実行直前に動作します。承認イベントが発行される前に実行するには、`RealtimeRunner(..., config={"tool_execution": {"pre_approval_tool_input_guardrails": True}})` を使用してランナーを作成します。この承認前チェックを通過した呼び出しも、承認後の実行前に再度チェックされます。

```python
async for event in session:
    if event.type == "tool_approval_required":
        await session.approve_tool_call(event.call_id)
```

具体的なサーバー側の承認ループについては、[`examples/realtime/app/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/app/server.py) を参照してください。ヒューマンインザループのドキュメントでも、[ヒューマンインザループ](../human_in_the_loop.md)でこのフローを参照しています。

### ハンドオフ

Realtime ハンドオフを使用すると、あるエージェントから別の専門エージェントへライブ会話を転送できます。

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

`RealtimeAgent` を直接指定したハンドオフは自動的にラップされます。また、`realtime_handoff(...)` を使用すると、名前、説明、検証、コールバック、利用可否をカスタマイズできます。Realtime ハンドオフは、通常のハンドオフの `input_filter` をサポートして **いません**。

### ガードレール

Realtime エージェントは、エージェントのレスポンスに対する出力ガードレールと、関数ツール呼び出しに対する入力ガードレールをサポートしています。出力ガードレールは、部分トークンごとではなく、デバウンスされた文字起こしの累積に対して動作し、例外を発生させる代わりに `guardrail_tripped` を発行します。

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

Realtime 出力ガードレールが作動すると、セッションはアクティブなレスポンスを中断し、`response.cancel` を強制的に実行して `guardrail_tripped` を発行します。さらに、作動したガードレールの名前を含む後続のユーザーメッセージを送信し、モデルが代替レスポンスを生成できるようにします。音声プレイヤーでは引き続き `audio_interrupted` を監視し、ローカル再生を即座に停止する必要があります。ガードレールはデバウンスされた文字起こしテキストに対して動作するため、トリップワイヤーが作動した時点ですでに一部の音声がバッファリングされている可能性があります。

## SIP とテレフォニー

Python SDK には、[`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] を使用する正式な SIP 接続フローが含まれています。

Realtime Calls API を介して着信があり、生成された `call_id` にエージェントセッションを接続する場合に使用します。

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

最初に通話を受け入れる必要があり、受け入れ時のペイロードをエージェントから導出されたセッション設定に一致させたい場合は、`OpenAIRealtimeSIPModel.build_initial_session_payload(...)` を使用します。完全なフローについては、[`examples/realtime/twilio_sip/server.py`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip/server.py) を参照してください。

## 低レベルアクセスとカスタムエンドポイント

`session.model` を介して、基盤となるトランスポートオブジェクトにアクセスできます。

次の場合に使用します。

-   `session.model.add_listener(...)` を介したカスタムリスナー
-   `response.create` や `session.update` などの raw クライアントイベント
-   `model_config` を介したカスタムの `url`、`headers`、`api_key` の処理
-   既存の Realtime 通話への `call_id` 接続

`RealtimeModelConfig` は次をサポートしています。

-   `api_key`
-   `url`
-   `headers`
-   `initial_model_settings`
-   `playback_tracker`
-   `call_id`

このリポジトリに含まれる `call_id` のコード例は SIP 用です。より広範な Realtime API でも、一部のサーバー側制御フローに `call_id` が使用されますが、ここでは Python のコード例としてパッケージ化されていません。

Azure OpenAI に接続する場合は、GA 版の Realtime エンドポイント URL と明示的なヘッダーを渡します。次に例を示します。

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"api-key": "<your-azure-api-key>"},
    }
)
```

トークンベース認証では、`headers` 内でベアラートークンを使用します。

```python
session = await runner.run(
    model_config={
        "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
        "headers": {"authorization": f"Bearer {token}"},
    }
)
```

`headers` を渡した場合、SDK は `Authorization` を自動的に追加しません。Realtime エージェントでは、従来のベータ版パス（`/openai/realtime?api-version=...`）を使用しないでください。

## 関連情報

-   [Realtime トランスポート](transport.md)
-   [クイックスタート](quickstart.md)
-   [OpenAI Realtime の会話](https://developers.openai.com/api/docs/guides/realtime-conversations/)
-   [OpenAI Realtime のサーバー側制御](https://developers.openai.com/api/docs/guides/realtime-server-controls/)
-   [`examples/realtime`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime)