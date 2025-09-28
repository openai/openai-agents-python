---
search:
  exclude: true
---
# ガイド

このガイドでは、OpenAI Agents SDK の realtime 機能を使って音声対応の AI エージェントを構築する方法を詳しく説明します。

!!! warning "Beta feature"
Realtime エージェントはベータ版です。実装の改善に伴い、後方互換性のない変更が発生する可能性があります。

## 概要

Realtime エージェントは、リアルタイムで音声とテキストの入力を処理し、リアルタイム音声で応答する会話フローを可能にします。OpenAI の Realtime API との永続的な接続を維持し、低レイテンシで自然な音声会話を実現し、割り込みにも適切に対応します。

## アーキテクチャ

### コアコンポーネント

realtime システムはいくつかの主要コンポーネントで構成されます。

-  **RealtimeAgent**: instructions、tools、handoffs を設定した エージェント。
-  **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
-  **RealtimeSession**: 単一の対話セッション。通常、ユーザー が会話を開始するたびに 1 つ作成し、会話が終了するまで維持します。
-  **RealtimeModel**: 基盤となるモデルのインターフェース（通常は OpenAI の WebSocket 実装）

### セッションフロー

一般的な realtime セッションは次のフローに従います。

1.  **RealtimeAgent を作成** し、instructions、tools、handoffs を設定します。
2.  **RealtimeRunner をセットアップ** し、エージェントと設定オプションを指定します。
3.  `await runner.run()` を使って **セッションを開始** し、RealtimeSession を取得します。
4.  `send_audio()` または `send_message()` を使って **音声またはテキストメッセージを送信** します。
5.  セッションを反復処理して **イベントをリッスン** します。イベントには音声出力、文字起こし、ツール呼び出し、ハンドオフ、エラーが含まれます。
6.  ユーザー がエージェントの発話中に話し始めた場合の **割り込み処理** を行います。現在の音声生成は自動的に停止します。

セッションは会話履歴を維持し、realtime モデルとの永続的な接続を管理します。

## エージェント設定

RealtimeAgent は通常の Agent クラスと同様に動作しますが、いくつか重要な違いがあります。API の詳細については、[`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスを参照してください。

通常のエージェントとの主な違い:

-  モデルの選択はエージェントレベルではなくセッションレベルで設定します。
-  structured outputs（`outputType` は未対応）はサポートされません。
-  音声はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。
-  それ以外の tools、handoffs、instructions などの機能は同様に動作します。

## セッション設定

### モデル設定

セッション設定では、基盤となる realtime モデルの動作を制御できます。モデル名（`gpt-realtime` など）、音声（alloy、echo、fable、onyx、nova、shimmer）の選択、対応するモダリティ（テキストおよび/または音声）を構成できます。音声フォーマットは入力と出力の両方で設定でき、デフォルトは PCM16 です。

### 音声設定

音声設定では、セッションが音声の入出力をどのように扱うかを制御します。Whisper のようなモデルを使った入力音声の文字起こし、言語設定、ドメイン固有用語の精度を高めるための文字起こしプロンプトを構成できます。ターン検出設定により、エージェント がいつ応答を開始・停止すべきかを制御でき、音声活動検出のしきい値、無音時間、検出された発話の前後パディングなどのオプションがあります。

## ツールと関数

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

ハンドオフ により、会話を専門特化した エージェント 間で引き継ぐことができます。

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

セッションはイベントを ストリーミング し、セッションオブジェクトを反復処理することでリッスンできます。イベントには音声出力チャンク、文字起こし結果、ツール実行の開始と終了、エージェントのハンドオフ、エラーが含まれます。対応すべき主なイベントは次のとおりです。

-  **audio**: エージェントの応答からの raw な音声データ
-  **audio_end**: エージェントの発話が終了
-  **audio_interrupted**: ユーザー がエージェントを割り込み
-  **tool_start/tool_end**: ツール実行のライフサイクル
-  **handoff**: エージェントのハンドオフが発生
-  **error**: 処理中にエラーが発生

完全なイベントの詳細は、[`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

realtime エージェントでサポートされるのは出力 ガードレール のみです。これらの ガードレール はデバウンスされ、（すべての単語ごとではなく）定期的に実行され、リアルタイム生成中のパフォーマンス問題を回避します。デフォルトのデバウンス長は 100 文字ですが、これは構成可能です。

ガードレール は `RealtimeAgent` に直接アタッチすることも、セッションの `run_config` 経由で提供することもできます。両方のソースからの ガードレール は同時に実行されます。

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

ガードレール がトリガーされると、`guardrail_tripped` イベントが生成され、エージェント の現在の応答を中断できます。デバウンス動作により、安全性とリアルタイム性能要件のバランスが取られます。テキスト エージェント と異なり、realtime エージェントは ガードレール がトリップしても **Exception を送出しません**。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使用して音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使用してテキストを送信します。

音声出力については、`audio` イベントをリッスンし、任意のオーディオライブラリでデータを再生します。ユーザー がエージェントを割り込んだ際に即時に再生を停止し、キューにある音声をクリアできるよう、`audio_interrupted` イベントも必ずリッスンしてください。

## 直接モデルアクセス

基盤となるモデルにアクセスして、カスタムリスナーの追加や高度な操作を実行できます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続の低レベル制御が必要な高度なユースケースに向けて、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## 例

動作する完全な code examples は、UI コンポーネントの有無それぞれのデモを含む [examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。