---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を使用して音声対応 AI エージェントを構築する方法を詳しく解説します。

!!! warning "Beta 機能"
Realtime エージェントは beta です。実装を改善するにあたり、破壊的変更が入る可能性があります。

## 概要

Realtime エージェントは会話フローを可能にし、音声およびテキスト入力をリアルタイムに処理して realtime 音声で応答します。OpenAI の Realtime API と永続的な接続を維持し、低レイテンシで自然な音声会話を実現するとともに、割り込みも適切に処理できます。

## アーキテクチャ

### コアコンポーネント

realtime システムは、いくつかの主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、handoffs で構成されたエージェントです。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッションです。通常は ユーザー が会話を開始するたびに 1 つ作成し、会話が完了するまで維持します。
-   **RealtimeModel**: 基盤となるモデル インターフェース（通常は OpenAI の WebSocket 実装）です。

### セッションフロー

一般的な realtime セッションは次のフローに従います。

1. instructions、tools、handoffs を指定して **RealtimeAgent(群) を作成** します。
2. エージェントと設定オプションで **RealtimeRunner をセットアップ** します。
3. `await runner.run()` を使用して **セッションを開始** すると、RealtimeSession が返ります。
4. `send_audio()` または `send_message()` を使用して、セッションへ **音声またはテキストメッセージを送信** します。
5. セッションを反復して **イベントをリッスン** します。イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザー がエージェントに被せて話す **割り込みを処理** します。これにより現在の音声生成が自動的に停止します。

セッションは会話履歴を維持し、realtime モデルとの永続的な接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスを参照してください。

通常の エージェント との主な違い:

-   モデル選択はエージェント レベルではなく、セッション レベルで設定します。
-   structured outputs はサポートされません（`outputType` はサポートされません）。
-   Voice はエージェントごとに設定できますが、最初のエージェントが話した後は変更できません。
-   tools、handoffs、instructions などのその他の機能は同様に動作します。

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの挙動を制御できます。モデル名（`gpt-realtime` など）、voice 選択（alloy、echo、fable、onyx、nova、shimmer）、対応 modality（テキスト および/または 音声）を設定できます。音声フォーマットは入力・出力の両方で設定でき、既定は PCM16 です。

### 音声設定

音声設定は、セッションが音声入力と出力をどのように扱うかを制御します。Whisper などのモデルを使用した入力音声の文字起こしを設定し、言語設定を指定し、ドメイン固有の用語の精度を向上させるための文字起こしプロンプトを提供できます。ターン検出設定では、エージェントが応答を開始・停止すべきタイミングを制御し、音声アクティビティ検出の閾値、無音時間、検出された音声の前後のパディングなどのオプションがあります。

## Tools と Functions

### ツールの追加

通常の エージェント と同様に、realtime エージェントは会話中に実行される 関数ツール をサポートします。

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

ハンドオフにより、会話を専門 エージェント 間で移管できます。

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

セッションはイベントをストリーミングし、セッション オブジェクトを反復することでリッスンできます。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始・終了、エージェントのハンドオフ、エラーが含まれます。処理すべき主要イベントは次のとおりです。

-   **audio**: エージェント応答の Raw 音声データ
-   **audio_end**: エージェントの発話終了
-   **audio_interrupted**: ユーザー がエージェントを割り込んだ
-   **tool_start/tool_end**: ツール実行ライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの完全な詳細は、[`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

realtime エージェントでは出力ガードレールのみがサポートされます。これらのガードレールはデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるため、（各単語ごとではなく）定期的に実行されます。既定のデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接付与するか、セッションの `run_config` 経由で提供できます。両方のソースのガードレールが一緒に実行されます。

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

ガードレールがトリガーされると `guardrail_tripped` イベントが生成され、エージェントの現在の応答を割り込めます。デバウンスの挙動は、安全性とリアルタイムのパフォーマンス要件のバランスを取るのに役立ちます。テキスト エージェント と異なり、realtime エージェントではガードレールがトリップしても Exception は **送出されません**。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声をセッションへ送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントをリッスンし、お使いの音声ライブラリで音声データを再生してください。ユーザー がエージェントを割り込んだときに直ちに再生を停止し、キューに溜まった音声をクリアできるよう、`audio_interrupted` イベントも必ずリッスンしてください。

## SIP 連携

[Realtime Calls API](https://platform.openai.com/docs/guides/realtime-sip) 経由で着信する電話に realtime エージェントを紐付けられます。SDK は [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] を提供しており、SIP でメディアをネゴシエートしつつ同じエージェントフローを再利用します。

使用するには、runner にモデル インスタンスを渡し、セッション開始時に SIP の `call_id` を指定します。call ID は着信を通知する webhook により配信されます。

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

発信者が電話を切ると SIP セッションが終了し、realtime 接続は自動的にクローズされます。完全な電話連携の例は、[`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) を参照してください。

## モデルへの直接アクセス

基盤となるモデルにアクセスして、カスタム リスナーを追加したり高度な操作を行ったりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続に対するより低レベルの制御が必要となる高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## 例

UI コンポーネントあり・なしのデモを含む、動作する完全な例については、[examples/realtime directory](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。

## Azure OpenAI エンドポイント形式

Azure OpenAI に接続する際は、GA Realtime のエンドポイント形式を使用し、`model_config` の headers で認証情報を渡してください。

```python
model_config = {
    "url": "wss://<your-resource>.openai.azure.com/openai/v1/realtime?model=<deployment-name>",
    "headers": {"api-key": "<your-azure-api-key>"},
}
```

トークンベースの auth では、`headers` に `{"authorization": f"Bearer {token}"}` を使用してください。