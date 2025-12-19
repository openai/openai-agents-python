---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を使って音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、破壊的な変更が発生する可能性があります。

## 概要

Realtime エージェントは、音声とテキスト入力をリアルタイムに処理し、リアルタイム音声で応答する会話フローを可能にします。OpenAI の Realtime API との永続的な接続を維持し、低レイテンシで自然な音声対話と、割り込みへの優雅な対応を実現します。

## アーキテクチャ

### コアコンポーネント

realtime システムは、いくつかの主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、handoffs を設定したエージェントです。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出すとセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッションです。通常、ユーザーが会話を開始するたびに作成し、会話が終了するまで維持します。
-   **RealtimeModel**: 基盤となるモデルのインターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

一般的な realtime セッションは次のフローに従います。

1. **RealtimeAgent を作成** し、instructions、tools、handoffs を設定します。
2. **RealtimeRunner をセットアップ** し、エージェントと構成オプションを指定します。
3. `await runner.run()` を使用して **セッションを開始** し、RealtimeSession を取得します。
4. `send_audio()` または `send_message()` を使用して **音声またはテキストメッセージを送信** します。
5. セッションを反復処理して **イベントをリッスン** します。イベントには、音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザーがエージェントの発話にかぶせて話したときに **割り込みを処理** します。現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつかの重要な違いがあります。API の詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご覧ください。

通常のエージェントとの主な違い:

-   モデル選択はエージェントレベルではなく、セッションレベルで設定します。
-   structured output はサポートされません（`outputType` は非対応）。
-   音声はエージェントごとに設定可能ですが、最初のエージェントが話し始めた後は変更できません。
-   ツール、ハンドオフ、instructions などのその他の機能は同様に機能します。

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの動作を制御できます。モデル名（`gpt-realtime` など）、ボイス選択（alloy、echo、fable、onyx、nova、shimmer）、対応モダリティ（テキストや音声）を設定できます。音声フォーマットは入力と出力の両方で設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定は、セッションが音声の入出力をどのように処理するかを制御します。Whisper などのモデルを使用した入力音声の文字起こし、言語設定、専門用語の精度を高めるための文字起こしプロンプトを設定できます。ターン検出設定では、音声活動検出のしきい値、無音時間、検出された音声の前後のパディングなど、エージェントがいつ応答を開始・停止するかを制御します。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、realtime エージェントは会話中に実行される 関数ツール をサポートします。

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

ハンドオフにより、専門特化したエージェント間で会話を転送できます。

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

セッションは、セッションオブジェクトを反復処理することでリッスン可能なイベントをストリーミングします。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始/終了、エージェントのハンドオフ、エラーが含まれます。特に処理すべき主なイベントは次のとおりです。

-   **audio**: エージェントの応答からの生の音声データ
-   **audio_end**: エージェントの発話が終了
-   **audio_interrupted**: ユーザーがエージェントを割り込み
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

Realtime エージェントでは出力ガードレールのみがサポートされます。パフォーマンス問題を避けるため、これらのガードレールはデバウンスされ、リアルタイム生成の最中に（毎語ではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` を通じて提供できます。両方のソースからのガードレールは併用されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を割り込む場合があります。デバウンス動作は、安全性とリアルタイム性能要件のバランスを保つのに役立ちます。テキストエージェントと異なり、realtime エージェントはガードレールがトリップしても例外を **発生させません**。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声を送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントをリッスンし、任意の音声ライブラリで再生します。ユーザーがエージェントを割り込んだ際に即座に再生を停止し、キューされた音声をクリアするため、`audio_interrupted` イベントも必ずリッスンしてください。

## SIP 連携

[Realtime Calls API](https://platform.openai.com/docs/guides/realtime-sip) によって着信する通話に realtime エージェントを接続できます。SDK は [`OpenAIRealtimeSIPModel`][agents.realtime.openai_realtime.OpenAIRealtimeSIPModel] を提供しており、同じエージェントフローを再利用しつつ、SIP 上でメディアをネゴシエートします。

使用するには、ランナーにモデルインスタンスを渡し、セッション開始時に SIP の `call_id` を指定します。コール ID は、着信を通知する Webhook によって送信されます。

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

発信者が電話を切ると、SIP セッションは終了し、realtime 接続は自動的に閉じられます。完全なテレフォニーのコード例は、[`examples/realtime/twilio_sip`](https://github.com/openai/openai-agents-python/tree/main/examples/realtime/twilio_sip) を参照してください。

## モデルへの直接アクセス

基盤となるモデルにアクセスし、カスタムリスナーの追加や高度な操作を実行できます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続をより低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全な動作するコード例については、UI コンポーネントあり/なしのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。