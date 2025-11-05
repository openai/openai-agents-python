---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を用いて音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、互換性が壊れる変更が発生する可能性があります。

## 概要

Realtime エージェントは、会話の流れを可能にし、音声とテキストの入力をリアルタイムで処理し、リアルタイム音声で応答します。OpenAI の Realtime API との永続的な接続を維持し、低レイテンシで自然な音声対話と、割り込みへのスムーズな対応を実現します。

## アーキテクチャ

### コアコンポーネント

realtime システムは、いくつかの主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、handoffs を設定したエージェント。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッション。通常、ユーザー が会話を開始するたびに作成し、会話が終了するまで維持します。
-   **RealtimeModel**: 基盤となるモデルのインターフェース（一般的には OpenAI の WebSocket 実装）

### セッションフロー

典型的な realtime セッションの流れは次のとおりです。

1. **RealtimeAgent を作成** し、instructions、tools、handoffs を設定します。
2. **RealtimeRunner を設定** し、エージェントと設定オプションを渡します。
3. **セッションを開始** します。`await runner.run()` を使用すると RealtimeSession が返ります。
4. **音声またはテキストメッセージを送信** します。`send_audio()` または `send_message()` を使用します。
5. **イベントを受信** します。セッションを反復処理して、音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーなどのイベントを受け取ります。
6. **割り込みを処理** します。ユーザー がエージェントの発話に被せて話すと、現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご覧ください。

通常のエージェントとの主な違い:

-   モデルの選択はエージェント レベルではなくセッション レベルで設定します。
-   structured output はサポートされません（`outputType` は非対応）。
-   ボイスはエージェントごとに設定できますが、最初のエージェントが発話した後は変更できません。
-   ツール、ハンドオフ、instructions などの他の機能は同様に動作します。

## セッション設定

### モデル設定

セッション設定により、基盤となる realtime モデルの動作を制御できます。モデル名（`gpt-realtime` など）、ボイス選択（alloy, echo, fable, onyx, nova, shimmer）、対応モダリティ（テキストおよび/または音声）を設定できます。音声フォーマットは入力・出力の両方で設定可能で、デフォルトは PCM16 です。

### オーディオ設定

オーディオ設定では、セッションが音声入力と出力をどのように扱うかを制御します。Whisper などのモデルを用いた入力音声の文字起こし、言語設定、ドメイン固有用語の精度を高めるための文字起こしプロンプトを設定できます。ターン検出では、エージェントがいつ応答を開始・終了すべきかを制御し、音声活動検出のしきい値、無音の長さ、検出された音声の前後のパディングなどのオプションがあります。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、realtime エージェントは会話中に実行される関数ツールをサポートします。

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

ハンドオフにより、特化したエージェント間で会話を引き継ぐことができます。

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

セッションはイベントをストリーミングし、セッションオブジェクトを反復処理することでリッスンできます。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始・終了、エージェントのハンドオフ、エラーが含まれます。特に処理すべき主要イベントは次のとおりです。

-   **audio**: エージェントの応答からの生の音声データ
-   **audio_end**: エージェントの発話が終了
-   **audio_interrupted**: ユーザー がエージェントを割り込み
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

Realtime エージェントでサポートされるのは出力ガードレールのみです。リアルタイム生成中のパフォーマンス問題を避けるため、これらのガードレールはデバウンスされ、（全単語ごとではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` で指定できます。両方のソースから提供されたガードレールは一緒に実行されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断することがあります。デバウンス動作により、安全性とリアルタイム性能要件のバランスを取ります。テキスト エージェントと異なり、realtime エージェントはガードレールに引っかかっても **Exception を** 発生させません。

## オーディオ処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] で音声を、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] でテキストをセッションに送信します。

音声出力については、`audio` イベントをリッスンして、任意の音声ライブラリで再生してください。ユーザー がエージェントを割り込んだ際に即座に再生を停止し、キュー済み音声をクリアするため、`audio_interrupted` イベントも必ずリッスンしてください。

## モデルへの直接アクセス

基盤となるモデルにアクセスして、カスタムリスナーの追加や高度な操作を実行できます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続をより低レベルに制御する必要がある高度なユースケースに対して、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全な動作コード例は、UI コンポーネントの有無それぞれのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。