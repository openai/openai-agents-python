---
search:
  exclude: true
---
# ガイド

このガイドでは、 OpenAI Agents SDK の realtime 機能を使って音声対応 AI エージェントを構築する方法を詳しく説明します。

!!! warning "ベータ機能"
Realtime エージェントはベータ版です。実装の改良に伴い、破壊的変更が発生する可能性があります。

## 概要

Realtime エージェントは、会話のフローを実現し、音声およびテキスト入力をリアルタイムに処理して、リアルタイム音声で応答します。OpenAI の Realtime API との永続的な接続を維持し、低レイテンシで自然な音声対話と、割り込みへのスムーズな対応を可能にします。

## アーキテクチャ

### 中核コンポーネント

realtime システムは、いくつかの重要なコンポーネントで構成されます。

-   **RealtimeAgent**: instructions、tools、ハンドオフで設定されたエージェント。
-   **RealtimeRunner**: 設定を管理します。`runner.run()` を呼び出してセッションを取得できます。
-   **RealtimeSession**: 単一の対話セッション。通常、ユーザーが会話を開始するたびに作成し、会話が終了するまで維持します。
-   **RealtimeModel**: 基盤となるモデルのインターフェース（一般的には OpenAI の WebSocket 実装）

### セッションの流れ

一般的な realtime セッションは次の流れに従います。

1. **RealtimeAgent を作成** し、instructions、tools、ハンドオフを設定します。
2. **RealtimeRunner を設定** し、エージェントと構成オプションを渡します。
3. **セッションを開始** します。`await runner.run()` を使用すると RealtimeSession が返ります。
4. **音声またはテキストメッセージを送信** します。`send_audio()` または `send_message()` を使用します。
5. **イベントをリッスン** します。セッションをイテレートして、音声出力、書き起こし、ツール呼び出し、ハンドオフ、エラーなどのイベントを受け取ります。
6. **割り込みに対応** します。ユーザーがエージェントの発話にかぶせた場合、進行中の音声生成は自動的に停止します。

セッションは会話履歴を保持し、realtime モデルとの永続接続を管理します。

## エージェントの設定

RealtimeAgent は通常の Agent クラスと類似していますが、いくつか重要な相違があります。完全な API の詳細は [`RealtimeAgent`][agents.realtime.agent.RealtimeAgent] の API リファレンスをご参照ください。

通常のエージェントとの主な違い:

-   モデル選択はエージェントではなくセッション単位で設定します。
-   structured outputs のサポートはありません（`outputType` は非対応）。
-   声質はエージェントごとに設定できますが、最初のエージェントが話し始めた後は変更できません。
-   ツール、ハンドオフ、instructions などのその他の機能は同様に動作します。

## セッションの設定

### モデル設定

セッション設定では、基盤となる realtime モデルの挙動を制御できます。モデル名（例: `gpt-4o-realtime-preview`）、音声の選択（alloy、echo、fable、onyx、nova、shimmer）、対応するモダリティ（テキストおよび/または音声）を設定できます。音声フォーマットは入力・出力の両方で指定でき、デフォルトは PCM16 です。

### 音声設定

音声設定は、セッションが音声の入出力をどのように扱うかを制御します。Whisper などのモデルを使った入力音声の書き起こし、言語設定、ドメイン固有用語の精度向上に役立つ書き起こしプロンプトを設定できます。ターン検出設定では、エージェントがいつ応答を開始・終了すべきかを制御でき、音声活動検出のしきい値、無音時間、検出された発話周辺のパディングなどを指定できます。

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

ハンドオフにより、会話を専門化されたエージェント間で引き継げます。

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

セッションはイベントをストリーミングし、セッションオブジェクトをイテレートしてリッスンできます。イベントには、音声出力チャンク、書き起こし結果、ツール実行の開始/終了、エージェントのハンドオフ、エラーが含まれます。特に扱うべき主なイベントは次のとおりです。

-   **audio**: エージェントの応答からの raw 音声データ
-   **audio_end**: エージェントの発話完了
-   **audio_interrupted**: ユーザーがエージェントを割り込み
-   **tool_start/tool_end**: ツール実行のライフサイクル
-   **handoff**: エージェントのハンドオフ発生
-   **error**: 処理中にエラーが発生

完全なイベントの詳細は [`RealtimeSessionEvent`][agents.realtime.events.RealtimeSessionEvent] を参照してください。

## ガードレール

realtime エージェントでサポートされるのは出力ガードレールのみです。これらのガードレールはデバウンスされ、リアルタイム生成中のパフォーマンス問題を避けるために（毎語ではなく）定期的に実行されます。デフォルトのデバウンス長は 100 文字ですが、設定可能です。

ガードレールは `RealtimeAgent` に直接アタッチするか、セッションの `run_config` を通じて提供できます。両方のソースのガードレールは一緒に実行されます。

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

ガードレールがトリガーされると、`guardrail_tripped` イベントが生成され、エージェントの現在の応答を中断することがあります。デバウンス動作は、安全性とリアルタイム性能要件のバランスを取るのに役立ちます。テキストエージェントと異なり、realtime エージェントはガードレールがトリップしても Exception をスローしません。

## 音声処理

[`session.send_audio(audio_bytes)`][agents.realtime.session.RealtimeSession.send_audio] を使って音声をセッションに送信するか、[`session.send_message()`][agents.realtime.session.RealtimeSession.send_message] を使ってテキストを送信します。

音声出力については、`audio` イベントをリッスンし、任意の音声ライブラリで再生してください。ユーザーがエージェントを割り込んだときに即座に再生を停止し、キュー済み音声をクリアするため、`audio_interrupted` イベントも必ずリッスンしてください。

## モデルへの直接アクセス

基盤となるモデルにアクセスし、カスタムリスナーを追加したり、高度な操作を実行したりできます。

```python
# Add a custom listener to the model
session.model.add_listener(my_custom_listener)
```

これにより、接続を低レベルで制御する必要がある高度なユースケース向けに、[`RealtimeModel`][agents.realtime.model.RealtimeModel] インターフェースへ直接アクセスできます。

## コード例

完全に動作するコード例は、[examples/realtime ディレクトリ](https://github.com/openai/openai-agents-python/tree/main/examples/realtime) を参照してください。UI コンポーネントの有無双方のデモが含まれています。