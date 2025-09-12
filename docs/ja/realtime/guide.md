---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を用いた音声対応 AI エージェントの構築について詳しく説明します。

!!! warning "Beta feature"
Realtime エージェントはベータ版です。実装改善に伴い、互換性に影響する変更が発生する可能性があります。

## 概要

Realtime エージェントは、会話のフローを可能にし、音声およびテキスト入力をリアルタイムに処理して、リアルタイム音声で応答します。OpenAI の Realtime API への永続接続を維持し、低遅延で自然な音声対話と、割り込みへの適切な対応を実現します。

## アーキテクチャ

### コアコンポーネント

realtime システムは、いくつかの主要なコンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、ハンドオフで構成されたエージェントです。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出すとセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッションです。通常、ユーザーが会話を開始するたびに 1 つ作成し、会話が終了するまで維持します。
-   **RealtimeModel**: 基盤となるモデルインターフェース（一般的には OpenAI の WebSocket 実装）

### セッションフロー

一般的な realtime セッションの流れは次のとおりです。

1. instructions、tools、ハンドオフを指定して **RealtimeAgent を作成** します。
2. エージェントと設定オプションを使って **RealtimeRunner を設定** します。
3. `await runner.run()` を使って **セッションを開始** します。これにより RealtimeSession が返されます。
4. `send_audio()` または `send_message()` を使用して **音声またはテキストメッセージを送信** します。
5. セッションを反復処理して **イベントを監視** します。イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザーがエージェントの発話にかぶせて話した場合の **割り込みを処理** します。現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェント設定

RealtimeAgent は、通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスを参照してください。

通常のエージェントとの主な違い:

-   モデル選択はエージェントではなくセッションレベルで設定します。
-   structured output はサポートされません（`outputType` は非対応）。
-   声質はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。
-   それ以外の機能（tools、ハンドオフ、instructions）は同様に動作します。

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの挙動を制御できます。モデル名（`gpt-realtime` など）、声質の選択（alloy、echo、fable、onyx、nova、shimmer）、対応モダリティ（テキストおよび／または音声）を設定できます。音声フォーマットは入力・出力の両方で設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定は、セッションが音声入力と出力をどのように処理するかを制御します。Whisper のようなモデルを用いた入力音声の文字起こし、言語設定、ドメイン固有用語の精度を高めるための文字起こしプロンプトを指定できます。発話区間検出（turn detection）の設定により、エージェントがいつ応答を開始・停止するかを制御でき、音声アクティビティ検出のしきい値、無音時間、検出された発話の前後パディングなどのオプションがあります。

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

ハンドオフを使用すると、専門化されたエージェント間で会話を引き継げます。

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

セッションはイベントをストリーミング出力し、セッションオブジェクトを反復処理することでそれらを監視できます。イベントには、音声出力チャンク、文字起こしの結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。主に処理すべきイベントは次のとおりです。

-   **audio**: エージェントの応答からの raw 音声データ
-   **audio_end**: エージェントの発話が完了
-   **audio_interrupted**: ユーザーがエージェントを割り込み
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

realtime エージェントでサポートされるのは出力ガードレールのみです。性能問題を避けるため、これらのガードレールはデバウンスされ、リアルタイム生成中に毎語ではなく定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

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

ガードレールが発動すると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を割り込むことがあります。デバウンス動作により、安全性とリアルタイム性能要件のバランスを取ります。テキストエージェントと異なり、realtime エージェントはガードレール発動時に Exception を送出しません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使って音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使ってテキストを送信します。

音声出力については、`audio` イベントを監視し、任意の音声ライブラリで音声データを再生してください。ユーザーがエージェントを割り込んだ際に即座に再生を停止し、キューされている音声をクリアできるよう、`audio_interrupted` イベントを必ず監視してください。

## 直接的なモデルアクセス

基盤となるモデルにアクセスして、カスタムリスナーを追加したり高度な操作を実行できます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケースに向けて、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全な動作サンプルは、UI コンポーネントあり・なしのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。