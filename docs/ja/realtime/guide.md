---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK の realtime 機能を使用して音声対応 AI エージェントを構築する方法を詳しく解説します。

!!! warning "Beta feature"
realtime エージェントはベータ版です。実装を改善するにつれて、破壊的変更が入る可能性があります。

## 概要

realtime エージェントは会話フローを実現し、音声とテキストの入力をリアルタイムに処理して、 realtime 音声で応答します。 OpenAI の Realtime API との永続的な接続を維持し、低レイテンシで自然な音声会話を可能にするとともに、割り込みにも適切に対応できます。

## アーキテクチャ

### コアコンポーネント

realtime システムは、いくつかの主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、handoffs で構成されたエージェントです。
-   **RealtimeRunner**: 設定を管理します。 `runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッションです。通常、ユーザーが会話を開始するたびに 1 つ作成し、会話が終わるまで維持します。
-   **RealtimeModel**: 基盤となるモデルインターフェース（通常は OpenAI の WebSocket 実装）です。

### セッションフロー

典型的な realtime セッションは次のフローに従います。

1. instructions、tools、handoffs を指定して **RealtimeAgent を作成**します。
2. エージェントと設定オプションを使って **RealtimeRunner をセットアップ**します。
3. `await runner.run()` を使用して **セッションを開始**します。これは RealtimeSession を返します。
4. `send_audio()` または `send_message()` を使って **音声またはテキストメッセージを送信**します。
5. セッションを反復して **イベントをリッスン**します。イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザーがエージェントの発話に重ねて話したときに **割り込みを処理**します。これにより現在の音声生成が自動的に停止します。

セッションは会話履歴を保持し、 realtime モデルとの永続的な接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。 API の詳細は、 [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスを参照してください。

通常のエージェントとの主な違い:

-   モデル選択はエージェントレベルではなく、セッションレベルで設定します。
-   structured output はサポートされません（`outputType` はサポートされません）。
-   音声はエージェントごとに設定できますが、最初のエージェントが話した後は変更できません。
-   tools、handoffs、instructions などのその他の機能は同じように動作します。

## セッション設定

### モデル設定

セッション設定により、基盤となる realtime モデルの挙動を制御できます。モデル名（`gpt-realtime` など）、音声選択（alloy、echo、fable、onyx、nova、shimmer）、対応モダリティ（テキストおよび/または音声）を設定できます。音声フォーマットは入力と出力の両方で設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定は、セッションが音声入力・出力をどのように扱うかを制御します。 Whisper などのモデルを用いた入力音声の文字起こし、言語設定、専門用語の精度を高めるための文字起こしプロンプトを設定できます。ターン検出設定では、エージェントが応答を開始・停止するタイミングを制御し、音声活動検出のしきい値、無音時間、検出された発話前後のパディングなどのオプションを指定できます。

`RealtimeRunner(config=...)` で設定できる追加のオプションには、次が含まれます。

-   `model_settings.output_modalities` で出力をテキストおよび/または音声に制約します。
-   `model_settings.input_audio_noise_reduction` で近距離または遠距離音声向けのノイズ抑制を調整します。
-   `guardrails_settings.debounce_text_length` で出力ガードレールの実行頻度を制御します。
-   `async_tool_calls` で関数ツールを並行実行します。
-   `tool_error_formatter` でモデルに見えるツールエラーメッセージをカスタマイズします。

型付き設定の全体像は、 [`RealtimeRunConfig`][agents.realtime.config.RealtimeRunConfig] と [`RealtimeSessionModelSettings`][agents.realtime.config.RealtimeSessionModelSettings] を参照してください。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、 realtime エージェントは会話中に実行される関数ツールをサポートします。

```python
from agents import function_tool

@function_tool
def get_weather(city: str) -> str:
    """Get current weather for a city."""
    # Your weather API logic here
    return f"The weather in {city} is sunny, 72°F"

@function_tool
def book_appointment(date: str, time: str, service: str) -> str:
    """Book an appointment."""
    # Your booking logic here
    return f"Appointment booked for {service} on {date} at {time}"

agent = RealtimeAgent(
    name="Assistant",
    instructions="You can help with weather and appointments.",
    tools=[get_weather, book_appointment],
)
```

## ハンドオフ

### ハンドオフの作成

ハンドオフにより、専門化されたエージェント間で会話を引き継げます。

```python
from agents.realtime import realtime_handoff

# Specialized agents
billing_agent = RealtimeAgent(
    name="Billing Support",
    instructions="You specialize in billing and payment issues.",
)

technical_agent = RealtimeAgent(
    name="Technical Support",
    instructions="You handle technical troubleshooting.",
)

# Main agent with handoffs
main_agent = RealtimeAgent(
    name="Customer Service",
    instructions="You are the main customer service agent. Hand off to specialists when needed.",
    handoffs=[
        realtime_handoff(billing_agent, tool_description="Transfer to billing support"),
        realtime_handoff(technical_agent, tool_description="Transfer to technical support"),
    ]
)
```

## イベント処理

セッションはイベントをストリーミングし、セッションオブジェクトを反復することでリッスンできます。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。処理すべき主なイベントは次のとおりです。

-   **audio**: エージェント応答の raw 音声データ
-   **audio_end**: エージェントの発話完了
-   **audio_interrupted**: ユーザーがエージェントを割り込んだ
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの詳細は、 [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

realtime エージェントでは、出力ガードレールのみがサポートされます。これらのガードレールはデバウンスされ、リアルタイム生成中の性能問題を避けるために（単語ごとではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` 経由で提供できます。両方のソースのガードレールが一緒に実行されます。

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

ガードレールがトリガーされると、 `guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できる場合があります。デバウンスの挙動は、安全性とリアルタイム性能要件のバランスに役立ちます。テキストエージェントとは異なり、 realtime エージェントではガードレールが作動しても Exception を **発生させません**。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用してセッションに音声を送信するか、 [`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については `audio` イベントをリッスンし、好みの音声ライブラリで音声データを再生してください。ユーザーがエージェントを割り込んだときに直ちに再生を停止し、キューに入っている音声をクリアするため、 `audio_interrupted` イベントを必ずリッスンしてください。

## SIP 統合

[Realtime Calls API](https://platform.openai.com/docs/guides/realtime-sip) 経由で着信する電話に realtime エージェントを接続できます。 SDK は [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] を提供しており、 SIP でメディアをネゴシエートしながら同じエージェントフローを再利用します。

使用するには、ランナーにモデルインスタンスを渡し、セッション開始時に SIP の `call_id` を指定します。コール ID は、着信を通知する webhook によって配信されます。

```python
from agents.realtime import RealtimeAgent, RealtimeRunner
from agents.realtime.openai_realtime import OpenAIRealtimeSIPModel

runner = RealtimeRunner(
    starting_agent=agent,
    model=OpenAIRealtimeSIPModel(),
)

async with await runner.run(
    model_config={
        "call_id": call_id_from_webhook,
        "initial_model_settings": {
            "turn_detection": {"type": "semantic_vad", "interrupt_response": True},
        },
    },
) as session:
    async for event in session:
        ...
```

発信者が通話を切ると SIP セッションが終了し、 realtime 接続も自動的に閉じます。電話の完全な例は、 [`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) を参照してください。

## モデルへの直接アクセス

カスタムリスナーの追加や高度な操作を行うために、基盤となるモデルにアクセスできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続をより低レベルで制御する必要がある高度なユースケース向けに、 [`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## 例

UI コンポーネントの有無を含むデモが入った完全に動作する例については、 [examples/realtime directory](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。

## Azure OpenAI エンドポイント形式

Azure OpenAI に接続する際は GA Realtime のエンドポイント形式を使用し、 `model_config` の `headers` で認証情報を渡してください。

```python
model_config = {
    "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
    "headers": {"api-key": "<your-azure-api-key>"},
}
```

トークンベース認証の場合は、 `headers` に `{"authorization": f"Bearer {token}"}` を使用します。