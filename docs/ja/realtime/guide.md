---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK の realtime 機能を用いて音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改善に伴い、互換性のない変更が発生する可能性があります。

## 概要

Realtime エージェントは、会話フローを可能にし、音声とテキストの入力をリアルタイムに処理して、リアルタイム音声で応答します。OpenAI の Realtime API との永続的な接続を維持し、低レイテンシで自然な音声会話と割り込みへの優雅な対応を実現します。

## アーキテクチャ

### コアコンポーネント

realtime システムは、いくつかの主要コンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、ハンドオフで構成されたエージェント。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一のインタラクションセッション。通常は ユーザー が会話を開始するたびに作成し、会話が終了するまで保持します。
-   **RealtimeModel**: 基盤となるモデルインターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

一般的な realtime セッションは次のフローに従います。

1. instructions、tools、ハンドオフを用いて **RealtimeAgent を作成** します。
2. エージェントと構成オプションで **RealtimeRunner をセットアップ** します。
3. `await runner.run()` を使用して **セッションを開始** し、RealtimeSession を取得します。
4. `send_audio()` または `send_message()` を使用して **音声またはテキストメッセージを送信** します。
5. セッションを反復処理して **イベントをリッスン** します。イベントには音声出力、トランスクリプト、ツール呼び出し、ハンドオフ、エラーが含まれます。
6. ユーザー がエージェントの発話に被せて話す **割り込みを処理** します。これにより、現在の音声生成は自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェントの設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。API の詳細は、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご参照ください。

通常のエージェントとの主な違い:

-   モデルの選択はエージェントレベルではなく、セッションレベルで設定します。
-   structured output のサポートはありません（`outputType` はサポートされません）。
-   音声はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。
-   ツール、ハンドオフ、instructions など、その他の機能は同じように動作します。

## セッションの設定

### モデル設定

セッション設定では、基礎となる realtime モデルの動作を制御できます。モデル名（`gpt-4o-realtime-preview` など）、音声の選択（alloy、echo、fable、onyx、nova、shimmer）、およびサポートするモダリティ（テキストおよび/または音声）を設定できます。音声フォーマットは入力・出力の両方で設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定は、セッションが音声入力と出力をどのように扱うかを制御します。Whisper などのモデルを使用した入力音声の文字起こし、言語設定、専門用語の精度を高めるためのトランスクリプションプロンプトを設定できます。ターン検出設定では、エージェントが応答を開始・終了すべきタイミングを制御でき、音声活動検出のしきい値、無音時間、検出された発話周辺のパディングなどのオプションがあります。

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

ハンドオフにより、会話を専門化されたエージェント間で引き継ぐことができます。

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

セッションはイベントを ストリーミング し、セッションオブジェクトを反復処理してリッスンできます。イベントには、音声出力チャンク、文字起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。主に処理すべきイベントは次のとおりです。

-   **audio**: エージェントの応答からの raw の音声データ
-   **audio_end**: エージェントの発話が完了
-   **audio_interrupted**: ユーザー がエージェントを割り込み
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフが発生
-   **error**: 処理中にエラーが発生

イベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

realtime エージェントでサポートされるのは出力 ガードレール のみです。これらの ガードレール はデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるために（毎語ではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` 経由で提供できます。両方のソースの ガードレール は一緒に実行されます。

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

ガードレール がトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を割り込むことがあります。デバウンスの挙動は、安全性とリアルタイムの性能要件のバランスを取るのに役立ちます。テキストエージェントと異なり、realtime エージェントは ガードレール が作動しても 例外 をスローしません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントをリッスンして、任意の音声ライブラリで音声データを再生します。ユーザー がエージェントを割り込んだ際に、再生を即座に停止してキュー済み音声をクリアするため、`audio_interrupted` イベントを必ずリッスンしてください。

## 直接的なモデルアクセス

基盤となるモデルにアクセスして、カスタムリスナーを追加したり高度な操作を実行したりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## code examples

完全な動作する code examples は、UI コンポーネントの有無それぞれのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) をご覧ください。