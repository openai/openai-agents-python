---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を用いて音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、破壊的変更が入る可能性があります。

## 概要

Realtime エージェントは、音声およびテキスト入力をリアルタイムに処理し、リアルタイム音声で応答する会話フローを可能にします。OpenAI の Realtime API との永続接続を維持し、低レイテンシで自然な音声会話や割り込みへのスムーズな対応を実現します。

## アーキテクチャ

### 中核コンポーネント

realtime システムは、次の主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、ハンドオフで構成されたエージェントです。
-   **RealtimeRunner**: 構成を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッションです。通常は ユーザー が会話を開始するたびに作成し、会話が終了するまで保持します。
-   **RealtimeModel**: 基盤となるモデルのインターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

典型的な realtime セッションは次のフローに従います。

1. instructions、tools、ハンドオフを用いて **RealtimeAgent を作成** します。
2. エージェントと構成オプションで **RealtimeRunner をセットアップ** します。
3. `await runner.run()` を使って **セッションを開始** し、RealtimeSession を取得します。
4. `send_audio()` または `send_message()` で **音声またはテキストメッセージを送信** します。
5. セッションを反復処理して **イベントをリッスン** します。イベントには、音声出力、トランスクリプト、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザー がエージェントの発話に被せて話した際の **割り込み処理** を行います。これにより現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェント構成

RealtimeAgent は通常の Agent クラスとほぼ同様に動作しますが、いくつか重要な違いがあります。API の詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご覧ください。

通常のエージェントとの主な違い:

-   モデルの選択はエージェント レベルではなくセッション レベルで構成します。
-   structured outputs のサポートはありません（`outputType` は未対応です）。
-   音声はエージェントごとに設定できますが、最初のエージェントが発話した後は変更できません。
-   それ以外の機能（tools、ハンドオフ、instructions）は同様に動作します。

## セッション構成

### モデル設定

セッション構成では、基盤となる realtime モデルの動作を制御できます。モデル名（例：`gpt-realtime`）、音声の選択（ alloy、echo、fable、onyx、nova、shimmer ）、およびサポートするモダリティ（テキストおよび/または音声）を設定できます。音声フォーマットは入力・出力の両方で設定でき、デフォルトは PCM16 です。

### オーディオ設定

オーディオ設定は、セッションが音声入出力をどのように扱うかを制御します。Whisper のようなモデルを使った入力音声の文字起こし、言語設定、専門用語の精度を高めるためのトランスクリプションプロンプトの指定が可能です。ターン検出設定では、エージェントがいつ応答を開始・終了すべきかを制御し、音声活動検出のしきい値、無音時間、検出音声の前後パディングなどを調整できます。

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

ハンドオフにより、専門化されたエージェント間で会話を移譲できます。

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

セッションはイベントを ストリーミング し、セッションオブジェクトを反復処理することでリッスンできます。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーなどが含まれます。重要なイベントは次のとおりです。

-   **audio**: エージェントの応答からの raw 音声データ
-   **audio_end**: エージェントの発話が終了
-   **audio_interrupted**: ユーザー によるエージェントの割り込み
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの完全な詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

Realtime エージェントでは出力ガードレールのみがサポートされています。パフォーマンス問題を避けるため、これらのガードレールはデバウンスされ、（毎語ではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、変更可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` を通じて提供できます。両方のソースのガードレールは併用されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントを生成し、エージェントの現在の応答を中断できます。デバウンス動作は、安全性とリアルタイムのパフォーマンス要件のバランスを取るのに役立ちます。テキスト エージェントと異なり、realtime エージェントはガードレールが作動しても **Exception**（例外） をスローしません。

## オーディオ処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使ってセッションに音声を送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使ってテキストを送信します。

音声出力については、`audio` イベントをリッスンし、好みのオーディオライブラリで音声データを再生してください。ユーザー がエージェントを割り込んだ際に即座に再生を停止し、キュー済みの音声をクリアするため、`audio_interrupted` イベントも必ずリッスンしてください。

## モデルへの直接アクセス

基盤となるモデルにアクセスして、カスタムリスナーを追加したり高度な操作を実行したりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これは、接続を低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスする手段を提供します。

## コード例

完全な動作コードは、UI コンポーネントあり/なしのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。