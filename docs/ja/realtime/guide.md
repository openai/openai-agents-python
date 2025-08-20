---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を用いて音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、互換性が壊れる変更が発生する可能性があります。

## 概要

Realtime エージェントは、音声とテキストの入力をリアルタイムに処理し、リアルタイム音声で応答する会話フローを可能にします。OpenAI の Realtime API と永続的な接続を維持し、低レイテンシで自然な音声対話と、割り込みへのスムーズな対応を実現します。

## アーキテクチャ

### コアコンポーネント

realtime システムは、いくつかの主要コンポーネントで構成されています。

-   **RealtimeAgent**: instructions、tools、ハンドオフで構成されたエージェント。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出すとセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッション。通常、ユーザーが会話を開始するたびに 1 つ作成し、会話が終了するまで維持します。
-   **RealtimeModel**: 基盤となるモデルのインターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

一般的な realtime セッションは次のフローに従います。

1. instructions、tools、ハンドオフを用いて **RealtimeAgent を作成** します。
2. エージェントと設定オプションで **RealtimeRunner をセットアップ** します。
3. `await runner.run()` を使って **セッションを開始** し、RealtimeSession を受け取ります。
4. `send_audio()` または `send_message()` を使って **音声またはテキストのメッセージを送信** します。
5. セッションを反復処理して **イベントをリッスン** します。イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザーがエージェントに被せて話した際の **割り込みを処理** します。これにより現在の音声生成が自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は、通常の Agent クラスと同様に動作しますが、いくつか重要な相違点があります。完全な API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご参照ください。

通常のエージェントとの主な違い:

-   モデルの選択はエージェントレベルではなくセッションレベルで設定します。
-   structured output はサポートされません（`outputType` はサポートされません）。
-   音声はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。
-   その他、tools、ハンドオフ、instructions などの機能は同様に機能します。

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの動作を制御できます。モデル名（例: `gpt-4o-realtime-preview`）、音声の選択（alloy、echo、fable、onyx、nova、shimmer）、対応するモダリティ（テキストや音声）を設定できます。音声フォーマットは入力と出力の両方で設定可能で、既定では PCM16 です。

### 音声設定

音声設定では、セッションの音声入力と出力の扱いを制御します。Whisper などのモデルを使った入力音声の文字起こし、言語設定、ドメイン固有用語の精度向上のための文字起こしプロンプトを設定できます。ターン検出の設定では、エージェントがいつ応答を開始・停止すべきかを制御でき、音声活動検出のしきい値、無音時間、検出された音声の前後におけるパディングなどを調整できます。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、realtime エージェントでも会話中に実行される 関数ツール をサポートします。

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

ハンドオフにより、会話を専門特化したエージェント間で引き継げます。

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

セッションは、セッションオブジェクトを反復処理することでリッスンできるイベントをストリーミングします。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。主に処理すべきイベントは以下です。

-   **audio**: エージェントの応答からの生の音声データ
-   **audio_end**: エージェントが話し終えた
-   **audio_interrupted**: ユーザーがエージェントを割り込んだ
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

完全なイベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

realtime エージェントでサポートされるのは出力 ガードレール のみです。これらのガードレールはデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるために（全単語ごとではなく）定期的に実行されます。既定のデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` で提供できます。両方のソースからのガードレールは一緒に実行されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できます。デバウンスの動作により、安全性とリアルタイム性能要件のバランスが取られます。テキストエージェントと異なり、realtime エージェントはガードレールが作動しても Exception を発生させません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使ってセッションに音声を送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使ってテキストを送信します。

音声出力については、`audio` イベントをリッスンして、任意の音声ライブラリで音声データを再生します。ユーザーがエージェントを割り込んだ際に即座に再生を停止し、キューにある音声をクリアするために、`audio_interrupted` イベントを必ずリッスンしてください。

## モデルへの直接アクセス

基盤となるモデルにアクセスして、カスタムリスナーを追加したり高度な操作を実行できます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケースに向けて、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全な動作するコード例は、UI コンポーネントあり・なしのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。