---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK の realtime 機能を使って音声対応 AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、破壊的変更が発生する可能性があります。

## 概要

Realtime エージェントは、会話型のフローを可能にし、音声とテキストの入力をリアルタイムに処理し、リアルタイム音声で応答します。 OpenAI の Realtime API との永続的な接続を維持し、低遅延で自然な音声対話と、割り込みへの優雅な対応を実現します。

## アーキテクチャ

### 中核コンポーネント

realtime システムは、いくつかの主要コンポーネントで構成されます。

-   ** RealtimeAgent **: instructions、tools、ハンドオフで構成されたエージェント。
-   ** RealtimeRunner **: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   ** RealtimeSession **: 1 回の対話セッション。通常、ユーザーが会話を開始するたびに 1 つ作成し、会話が終了するまで存続させます。
-   ** RealtimeModel **: 基盤となるモデルインターフェース（一般的には OpenAI の WebSocket 実装）

### セッションフロー

一般的な realtime セッションは次のフローに従います。

1. ** RealtimeAgent を作成 **: instructions、tools、ハンドオフを設定します。
2. ** RealtimeRunner をセットアップ **: エージェントと設定オプションで構成します。
3. ** セッションを開始 **: `await runner.run()` を使用し、 RealtimeSession が返されます。
4. ** 音声またはテキストメッセージを送信 **: `send_audio()` または `send_message()` を使用します。
5. ** イベントをリッスン **: セッションを反復処理してイベントを受け取ります。イベントには音声出力、字幕、ツール呼び出し、ハンドオフ、エラーなどが含まれます。
6. ** 割り込みへの対応 **: ユーザーがエージェントの発話にかぶせた場合、現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、 realtime モデルとの永続的な接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつかの重要な違いがあります。完全な API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスを参照してください。

通常のエージェントとの主な違い:

-   モデル選択はエージェントレベルではなく、セッションレベルで設定します。
-   structured outputs のサポートはありません（`outputType` はサポートされていません）。
-   音声はエージェントごとに設定できますが、最初のエージェントが発話した後は変更できません。
-   それ以外の機能（tools、ハンドオフ、instructions）は同様に動作します。

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの動作を制御できます。モデル名（例: `gpt-4o-realtime-preview`）、音声選択（alloy、echo、fable、onyx、nova、shimmer）、およびサポートするモダリティ（テキストや音声）を設定できます。音声フォーマットは入力・出力の両方に設定でき、既定は PCM16 です。

### 音声設定

音声設定では、セッションが音声入出力をどのように扱うかを制御します。 Whisper などのモデルを使った入力音声の文字起こし、言語設定、ドメイン固有語の精度向上のための文字起こしプロンプトを設定できます。ターン検出設定では、エージェントが応答を開始・終了するタイミングを制御し、音声活動検出のしきい値、無音の長さ、検出された発話周辺のパディングなどを指定できます。

## ツールと関数

### ツールの追加

通常のエージェントと同様に、 realtime エージェントは会話中に実行される 関数ツール をサポートします。

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

セッションは、セッションオブジェクトを反復処理することでリッスンできるイベントを ストリーミング します。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。重要なイベントは次のとおりです。

-   ** audio **: エージェントの応答からの raw な音声データ
-   ** audio_end **: エージェントの発話が終了
-   ** audio_interrupted **: ユーザーがエージェントを割り込み
-   ** tool_start/tool_end **: ツール実行のライフサイクル
-   ** handoff **: エージェントのハンドオフが発生
-   ** error **: 処理中にエラーが発生

イベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

realtime エージェントでは出力ガードレールのみがサポートされています。これらのガードレールはデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるために（毎語ではなく）定期的に実行されます。既定のデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` を介して提供できます。両方のソースのガードレールは併用されて実行されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断できます。デバウンス動作は、安全性とリアルタイム性能要件のバランスを取るのに役立ちます。テキストエージェントと異なり、 realtime エージェントはガードレール発火時に例外をスローしません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントをリッスンし、任意の音声ライブラリで音声データを再生してください。ユーザーがエージェントを割り込んだ際に即座に再生を停止し、キュー済み音声をクリアするため、`audio_interrupted` イベントも必ずリッスンしてください。

## モデルへの直接アクセス

独自のリスナーを追加したり、高度な操作を行うために、基盤となるモデルへアクセスできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続をより低レベルに制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全に動作するサンプルは、 UI コンポーネントの有無それぞれのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。